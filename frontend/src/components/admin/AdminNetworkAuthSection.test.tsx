// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminGetNetworkSettings: vi.fn(),
  adminGetMyIp: vi.fn(),
  adminGetAuthSettings: vi.fn(),
  adminListAuthUsers: vi.fn(),
  adminUpdateNetworkSettings: vi.fn(),
  adminUpdateAuthSettings: vi.fn(),
  adminAddAuthUser: vi.fn(),
  adminUpdateAuthUserPassword: vi.fn(),
  adminTestLdap: vi.fn(),
  adminDeleteAuthUser: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})
vi.mock('./UrlShareControl', () => ({ default: () => null }))

import { I18nProvider } from '../../lib/i18n'
import { ApiCodeError, RateLimitError } from '../../lib/api'
import type { AuthSettings, NetworkSettings } from '../../lib/api'
import AdminNetworkAuthSection from './AdminNetworkAuthSection'

// Locked by its own `disabled` or by a disabled <fieldset> around it (which a
// browser honours, though the control's own `disabled` stays false).
const isLocked = (el: Element) => (el as HTMLInputElement).disabled || el.closest('fieldset[disabled]') !== null

const NET: NetworkSettings = {
  ip_filter_enabled: false,
  ip_filter_default: 'allow',
  ip_rules: [],
  mdns_enabled: false,
  mdns_active: false,
  ga_measurement_id: '',
  analytics_mode: 'off',
  analytics_notice_toast_enabled: false,
  analytics_hash_salt_available: true,
}

const AUTH: AuthSettings = {
  mode: 'ldap',
  ldap_server_uri: 'ldap://ldap.example.org',
  ldap_bind_dn: '',
  ldap_bind_password_configured: false,
  ldap_user_base_dn: 'dc=example,dc=org',
  ldap_user_filter: '',
  ldap_use_starttls: false,
  google_client_id: '',
  google_client_secret_configured: false,
  google_allowed_domains: '',
  google_allowed_emails: '',
  session_ttl_seconds: 86400,
  login_message: '',
  login_button_label: '',
  lan_direct_noauth_mode: 'off',
}

function mount(onDirtyChange?: (dirty: boolean) => void) {
  return render(
    <I18nProvider>
      <AdminNetworkAuthSection onDirtyChange={onDirtyChange} />
    </I18nProvider>,
  )
}

const ldapServerInput = () =>
  screen.getByText('LDAP server URI').closest('label')!.querySelector('input')!

beforeEach(() => {
  localStorage.clear()
  window.history.replaceState(null, '', '/admin/dashboard')
  api.adminGetNetworkSettings.mockResolvedValue(NET)
  api.adminGetMyIp.mockResolvedValue({ ip: '127.0.0.1', via_cloudflare_tunnel: false })
  api.adminGetAuthSettings.mockResolvedValue(AUTH)
  api.adminListAuthUsers.mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.resetAllMocks()
})

describe('AdminNetworkAuthSection load failures', () => {
  it('shows why the network settings failed to load, with a retry', async () => {
    api.adminGetNetworkSettings.mockRejectedValueOnce(new RateLimitError())
    mount()
    expect(await screen.findByText('Failed to load network settings.')).toBeTruthy()
    expect(screen.getByText(/Too many requests/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Announce service via mDNS')).toBeTruthy()
  })

  it('shows why the authentication settings failed to load', async () => {
    api.adminGetAuthSettings.mockRejectedValueOnce(new Error('auth settings failed: 502'))
    mount()
    expect(await screen.findByText('Failed to load authentication settings.')).toBeTruthy()
    expect(screen.getByText('auth settings failed: 502')).toBeTruthy()
  })
})

describe('AdminNetworkAuthSection while saving', () => {
  it('locks the network form until the save answers', async () => {
    let finish: (v: unknown) => void = () => {}
    api.adminUpdateNetworkSettings.mockReturnValue(new Promise((r) => { finish = r }))
    mount()
    const mdns = (await screen.findByLabelText('Announce service via mDNS')) as HTMLInputElement
    fireEvent.click(mdns)
    fireEvent.click(screen.getByRole('button', { name: 'Save network settings' }))
    await waitFor(() => expect(api.adminUpdateNetworkSettings).toHaveBeenCalled())
    // Nothing can be edited while the PATCH is in flight...
    expect(mdns.matches(':disabled')).toBe(true)
    await act(async () => { finish({ ...NET, mdns_enabled: true, mdns_ok: true, mdns_msg: '' }) })
    // ...and the form is editable again once its reply has landed.
    expect(mdns.matches(':disabled')).toBe(false)
    expect(mdns.checked).toBe(true)
  })

  it('locks the authentication form until the save answers', async () => {
    let finish: (v: unknown) => void = () => {}
    api.adminUpdateAuthSettings.mockReturnValue(new Promise((r) => { finish = r }))
    mount()
    await screen.findByText('LDAP server URI')
    fireEvent.change(ldapServerInput(), { target: { value: 'ldaps://ldap.example.org' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save authentication settings' }))
    await waitFor(() => expect(api.adminUpdateAuthSettings).toHaveBeenCalled())
    expect(ldapServerInput().matches(':disabled')).toBe(true)
    await act(async () => { finish({ ...AUTH, ldap_server_uri: 'ldaps://ldap.example.org', warning: '' }) })
    expect(ldapServerInput().matches(':disabled')).toBe(false)
  })
})

describe('AdminNetworkAuthSection unsaved edits', () => {
  it('reports dirty to the dashboard and asks before browser Back leaves', async () => {
    const confirmSpy = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmSpy)
    window.history.pushState(null, '', '/admin/dashboard')
    const onDirty = vi.fn()
    mount(onDirty)
    fireEvent.click(await screen.findByLabelText('Announce service via mDNS'))
    await waitFor(() => expect(onDirty).toHaveBeenLastCalledWith(true))
    await act(async () => { window.history.back() })
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    // Cancelled: still here with the edit in place.
    expect(window.location.pathname).toBe('/admin/dashboard')
    expect((screen.getByLabelText('Announce service via mDNS') as HTMLInputElement).checked).toBe(true)
  })

  it('hands the dashboard an exit that leaves no guard entry for Back to land on', async () => {
    window.history.replaceState(null, '', '/admin')
    window.history.pushState(null, '', '/admin/dashboard')
    const leaveRef: { current: ((go: () => void) => void) | null } = { current: null }
    const view = render(
      <I18nProvider>
        <AdminNetworkAuthSection leaveRef={leaveRef} />
      </I18nProvider>,
    )
    fireEvent.click(await screen.findByLabelText('Announce service via mDNS'))
    await waitFor(() => expect(window.history.state?.wsiNavGuard).toBeTruthy())
    expect(leaveRef.current).not.toBeNull()

    // A confirmed header link: the dashboard runs its navigation through it.
    act(() => leaveRef.current!(() => window.history.pushState(null, '', '/admin/account')))
    await waitFor(() => expect(window.location.pathname).toBe('/admin/account'))
    view.unmount()
    expect(leaveRef.current).toBeNull()

    // One Back returns to the dashboard's own entry, the next one leaves it.
    await act(async () => { window.history.back() })
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(window.history.state?.wsiNavGuard).toBeUndefined()
    await act(async () => { window.history.back() })
    await waitFor(() => expect(window.location.pathname).toBe('/admin'))
  })
})

describe('AdminNetworkAuthSection "Sign everyone out"', () => {
  it('sends only the revoke flag after a confirm and leaves unsaved edits alone', async () => {
    const confirmSpy = vi.fn(() => true)
    vi.stubGlobal('confirm', confirmSpy)
    api.adminUpdateAuthSettings.mockResolvedValue({ ...AUTH, warning: '' })
    mount()
    await screen.findByText('LDAP server URI')
    fireEvent.change(ldapServerInput(), { target: { value: 'ldaps://edited.example.org' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign everyone out' }))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(api.adminUpdateAuthSettings).toHaveBeenCalledWith({ revoke_app_sessions: true }))
    expect(await screen.findByText('Every visitor has been signed out.')).toBeTruthy()
    // The edit is neither saved nor reverted by the reply.
    expect(ldapServerInput().value).toBe('ldaps://edited.example.org')
    expect(screen.getAllByText('Unsaved changes').length).toBeGreaterThan(0)
  })

  it('does nothing when the confirm is cancelled, and reports a failure', async () => {
    const confirmSpy = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmSpy)
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Sign everyone out' }))
    expect(api.adminUpdateAuthSettings).not.toHaveBeenCalled()

    confirmSpy.mockReturnValue(true)
    api.adminUpdateAuthSettings.mockRejectedValue(new RateLimitError())
    fireEvent.click(screen.getByRole('button', { name: 'Sign everyone out' }))
    expect(await screen.findByText(/^Could not sign everyone out: Too many requests/)).toBeTruthy()
  })

  it('is not offered while no login is required', async () => {
    api.adminGetAuthSettings.mockResolvedValue({ ...AUTH, mode: 'none' })
    mount()
    await screen.findByText('Session TTL (seconds)')
    expect(screen.queryByRole('button', { name: 'Sign everyone out' })).toBeNull()
  })
})

const inputUnder = (label: string) =>
  screen.getByText(label).closest('label')!.querySelector('input')!

describe('AdminNetworkAuthSection network save', () => {
  it('sends the edited settings, then reads clean against the reply', async () => {
    api.adminUpdateNetworkSettings.mockImplementation(async (p: Record<string, unknown>) => ({
      ...NET, ...p, mdns_active: true, mdns_ok: true, mdns_msg: '',
    }))
    mount()
    fireEvent.click(await screen.findByLabelText('Enable IP filter'))
    fireEvent.change(inputUnder('IP / CIDR'), { target: { value: '10.0.0.0/8' } })
    fireEvent.change(inputUnder('Note (optional)'), { target: { value: ' office ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    fireEvent.change(inputUnder('Measurement ID'), { target: { value: 'g-abc123' } })
    fireEvent.click(screen.getByLabelText('Announce service via mDNS'))
    expect(screen.getAllByText('Unsaved changes')).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Save network settings' }))
    await waitFor(() => expect(api.adminUpdateNetworkSettings).toHaveBeenCalledTimes(1))
    expect(api.adminUpdateNetworkSettings).toHaveBeenCalledWith({
      ip_filter_enabled: true,
      ip_filter_default: 'allow',
      ip_rules: [{ ip: '10.0.0.0/8', action: 'allow', note: 'office' }],
      mdns_enabled: true,
      ga_measurement_id: 'G-ABC123',
      analytics_mode: 'off',
      analytics_notice_toast_enabled: false,
      override_ip_lockout: false,
    })
    expect(await screen.findByText('Saved')).toBeTruthy()
    expect(screen.queryByText('Unsaved changes')).toBeNull()
  })

  it('refuses to save over a typed but not added rule', async () => {
    mount()
    fireEvent.click(await screen.findByLabelText('Enable IP filter'))
    fireEvent.change(inputUnder('IP / CIDR'), { target: { value: '10.0.0.1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save network settings' }))
    expect(await screen.findByText(/typed in the Add row/)).toBeTruthy()
    expect(api.adminUpdateNetworkSettings).not.toHaveBeenCalled()
  })

  it('asks before a deny default that would lock out the current IP, and overrides the guard once confirmed', async () => {
    const confirmSpy = vi.fn((_msg?: string) => true)
    vi.stubGlobal('confirm', confirmSpy)
    api.adminGetMyIp.mockResolvedValue({ ip: '203.0.113.7', via_cloudflare_tunnel: false })
    api.adminUpdateNetworkSettings.mockResolvedValue({
      ...NET, ip_filter_enabled: true, ip_filter_default: 'deny', mdns_ok: true, mdns_msg: '',
    })
    mount()
    fireEvent.click(await screen.findByLabelText('Enable IP filter'))
    const defaultSelect = screen.getByText('Default for unmatched rules').closest('label')!.querySelector('select')!
    fireEvent.change(defaultSelect, { target: { value: 'deny' } })
    await screen.findByText(/203\.0\.113\.7/)
    fireEvent.click(screen.getByRole('button', { name: 'Save network settings' }))
    await waitFor(() => expect(api.adminUpdateNetworkSettings).toHaveBeenCalledTimes(1))
    expect(confirmSpy.mock.calls[0][0]).toMatch(/203\.0\.113\.7/)
    expect(api.adminUpdateNetworkSettings.mock.calls[0][0])
      .toMatchObject({ ip_filter_default: 'deny', override_ip_lockout: true })
  })

  // Saves the loaded IP-filter rules after an unrelated edit (mDNS), from
  // `myIp`, and returns the confirm spy. `serverRefusesOnce` makes the first
  // PATCH fail the way the server's self-lockout guard refuses a save.
  async function saveRulesFrom(
    myIp: string,
    rules: NetworkSettings['ip_rules'],
    { confirm = true, serverRefusesOnce = false } = {},
  ) {
    const confirmSpy = vi.fn((_msg?: string) => confirm)
    vi.stubGlobal('confirm', confirmSpy)
    api.adminGetMyIp.mockResolvedValue({ ip: myIp, via_cloudflare_tunnel: false })
    api.adminGetNetworkSettings.mockResolvedValue({ ...NET, ip_filter_enabled: true, ip_rules: rules })
    api.adminUpdateNetworkSettings.mockImplementation(async (p: Record<string, unknown>) => ({
      ...NET, ...p, mdns_ok: true, mdns_msg: '',
    }))
    if (serverRefusesOnce) {
      api.adminUpdateNetworkSettings.mockRejectedValueOnce(
        new ApiCodeError('adminDash.apiErr.ipFilterSelfLockout', { ip: '2001:db8::5' }),
      )
    }
    mount()
    fireEvent.click(await screen.findByLabelText('Announce service via mDNS'))
    await screen.findByText(`+ Fill current IP (${myIp})`)
    fireEvent.click(screen.getByRole('button', { name: 'Save network settings' }))
    return confirmSpy
  }

  it('asks before an IPv6 CIDR deny rule that covers the current IP', async () => {
    const confirmSpy = await saveRulesFrom('2001:db8::5', [{ ip: '2001:db8::/32', action: 'deny', note: '' }])
    await waitFor(() => expect(api.adminUpdateNetworkSettings).toHaveBeenCalledTimes(1))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(confirmSpy.mock.calls[0][0]).toMatch(/"deny" rule matches your current IP \(2001:db8::5\)/)
    expect(api.adminUpdateNetworkSettings.mock.calls[0][0]).toMatchObject({ override_ip_lockout: true })
  })

  it('does not ask when an allow rule above the IPv6 CIDR deny covers the current IP', async () => {
    const confirmSpy = await saveRulesFrom('2001:db8::5', [
      { ip: '2001:db8::5', action: 'allow', note: '' },
      { ip: '2001:db8::/32', action: 'deny', note: '' },
    ])
    await waitFor(() => expect(api.adminUpdateNetworkSettings).toHaveBeenCalledTimes(1))
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(api.adminUpdateNetworkSettings.mock.calls[0][0]).toMatchObject({ override_ip_lockout: false })
  })

  it('saves nothing when the IPv6 lockout warning is declined', async () => {
    const confirmSpy = await saveRulesFrom(
      '2001:db8::5', [{ ip: '2001:db8::/32', action: 'deny', note: '' }], { confirm: false },
    )
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    expect(api.adminUpdateNetworkSettings).not.toHaveBeenCalled()
  })

  it('asks and resends with the override when the server still refuses a lockout', async () => {
    const confirmSpy = await saveRulesFrom(
      '192.0.2.10', [{ ip: '2001:db8::/32', action: 'deny', note: '' }], { serverRefusesOnce: true },
    )
    await waitFor(() => expect(api.adminUpdateNetworkSettings).toHaveBeenCalledTimes(2))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(confirmSpy.mock.calls[0][0]).toMatch(/would block your current IP \(2001:db8::5\)/)
    expect(api.adminUpdateNetworkSettings.mock.calls[0][0]).toMatchObject({ override_ip_lockout: false })
    expect(api.adminUpdateNetworkSettings.mock.calls[1][0]).toMatchObject({ override_ip_lockout: true })
    expect(await screen.findByText('Saved')).toBeTruthy()
  })

  it('shows the translated refusal when the server-side lockout prompt is declined', async () => {
    const confirmSpy = await saveRulesFrom(
      '192.0.2.10', [{ ip: '2001:db8::/32', action: 'deny', note: '' }],
      { confirm: false, serverRefusesOnce: true },
    )
    expect(await screen.findByText(/^Error: Not saved: these rules would block your current IP \(2001:db8::5\)/)).toBeTruthy()
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(api.adminUpdateNetworkSettings).toHaveBeenCalledTimes(1)
  })
})

describe('AdminNetworkAuthSection authentication save', () => {
  it('sends a typed secret and the TTL, then clears the secret input and reads clean', async () => {
    api.adminUpdateAuthSettings.mockImplementation(async () => ({
      ...AUTH, ldap_bind_password_configured: true, session_ttl_seconds: 3600, warning: 'not ready',
    }))
    mount()
    await screen.findByText('LDAP server URI')
    fireEvent.change(inputUnder('Bind password'), { target: { value: 's3cret' } })
    fireEvent.change(inputUnder('Session TTL (seconds)'), { target: { value: '3600' } })
    expect(screen.getAllByText('Unsaved changes')).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Save authentication settings' }))
    await waitFor(() => expect(api.adminUpdateAuthSettings).toHaveBeenCalledTimes(1))
    expect(api.adminUpdateAuthSettings).toHaveBeenCalledWith({
      mode: 'ldap',
      ldap_server_uri: 'ldap://ldap.example.org',
      ldap_bind_dn: '',
      ldap_user_base_dn: 'dc=example,dc=org',
      ldap_user_filter: '',
      ldap_use_starttls: false,
      google_client_id: '',
      google_allowed_domains: '',
      google_allowed_emails: '',
      login_message: '',
      login_button_label: '',
      lan_direct_noauth_mode: 'off',
      ldap_bind_password: 's3cret',
      session_ttl_seconds: 3600,
    })
    expect(await screen.findByText('Authentication settings saved')).toBeTruthy()
    // The server's English warning is shown in the UI language.
    expect(screen.getByText(/LDAP mode is selected but the server URI/)).toBeTruthy()
    expect(inputUnder('Bind password').value).toBe('')
    expect(screen.queryByText('Unsaved changes')).toBeNull()
  })

  it('sends the individually allowed email addresses under Google OAuth', async () => {
    api.adminGetAuthSettings.mockResolvedValue({ ...AUTH, mode: 'google_oauth' })
    api.adminUpdateAuthSettings.mockImplementation(async (p: Record<string, unknown>) => ({
      ...AUTH, mode: 'google_oauth', ...p, warning: '',
    }))
    mount()
    const emailsInput = (await screen.findByText(/^Individually allowed email addresses/))
      .closest('label')!.querySelector('input')!
    fireEvent.change(emailsInput, { target: { value: 'friend@gmail.com' } })
    expect(screen.getAllByText('Unsaved changes')).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Save authentication settings' }))
    await waitFor(() => expect(api.adminUpdateAuthSettings).toHaveBeenCalledTimes(1))
    expect(api.adminUpdateAuthSettings.mock.calls[0][0])
      .toMatchObject({ mode: 'google_oauth', google_allowed_emails: 'friend@gmail.com' })
    expect(await screen.findByText('Authentication settings saved')).toBeTruthy()
    expect(screen.queryByText('Unsaved changes')).toBeNull()
  })
})

describe('AdminNetworkAuthSection panels', () => {
  it('reports either form being dirty, and clean again once the edit is undone', async () => {
    const onDirty = vi.fn()
    mount(onDirty)
    await screen.findByText('LDAP server URI')
    fireEvent.change(ldapServerInput(), { target: { value: 'ldaps://other' } })
    await waitFor(() => expect(onDirty).toHaveBeenLastCalledWith(true))
    fireEvent.change(ldapServerInput(), { target: { value: AUTH.ldap_server_uri } })
    await waitFor(() => expect(onDirty).toHaveBeenLastCalledWith(false))
  })

  it('keeps the authentication form usable when the network settings fail to load', async () => {
    api.adminGetNetworkSettings.mockRejectedValue(new RateLimitError())
    mount()
    expect(await screen.findByText('Failed to load network settings.')).toBeTruthy()
    expect(await screen.findByText('LDAP server URI')).toBeTruthy()
  })
})

describe('AdminNetworkAuthSection side actions while in flight', () => {
  // Each success clears (or closes) its fields, so they are locked until it
  // lands: text typed meanwhile would otherwise be wiped, or a change
  // reported as done for a value that was never sent.
  const deferred = () => {
    let resolve: (v: unknown) => void = () => {}
    const promise = new Promise((r) => { resolve = r })
    return { promise, resolve }
  }
  const fieldInput = (label: string) => screen.getAllByText(label)
    .map((el) => el.closest('label')?.querySelector('input'))
    .find(Boolean) as HTMLInputElement

  it('locks the add-user fields until the add lands', async () => {
    api.adminGetAuthSettings.mockResolvedValue({ ...AUTH, mode: 'basic' })
    const add = deferred()
    api.adminAddAuthUser.mockReturnValue(add.promise)
    mount()
    await screen.findByRole('button', { name: 'Add user' })
    const user = fieldInput('Username')
    const pw = user.closest('div')!.querySelector('input[type="password"]') as HTMLInputElement
    fireEvent.change(user, { target: { value: 'bob' } })
    fireEvent.change(pw, { target: { value: 'secret-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add user' }))
    await waitFor(() => expect(isLocked(user)).toBe(true))
    expect(isLocked(pw)).toBe(true)
    await act(async () => add.resolve({ id: 1, username: 'bob' }))
    expect(isLocked(user)).toBe(false)
    expect(user.value).toBe('')
  })

  it("locks a user's password change, and the other rows' toggles, until it lands", async () => {
    api.adminGetAuthSettings.mockResolvedValue({ ...AUTH, mode: 'basic' })
    api.adminListAuthUsers.mockResolvedValue([{ id: 1, username: 'alice' }, { id: 2, username: 'bob' }])
    const change = deferred()
    api.adminUpdateAuthUserPassword.mockReturnValue(change.promise)
    mount()
    const toggles = await screen.findAllByRole('button', { name: 'Change password' })
    fireEvent.click(toggles[0])
    const pw = screen.getByPlaceholderText('New password') as HTMLInputElement
    fireEvent.change(pw, { target: { value: 'pass-one' } })
    fireEvent.click(screen.getByRole('button', { name: 'Update' }))
    await waitFor(() => expect(isLocked(pw)).toBe(true))
    expect(api.adminUpdateAuthUserPassword).toHaveBeenCalledWith(1, 'pass-one')
    for (const b of screen.getAllByRole('button', { name: 'Change password' })) expect((b as HTMLButtonElement).disabled).toBe(true)
    await act(async () => change.resolve({}))
    expect(screen.queryByPlaceholderText('New password')).toBeNull()
  })

  it('locks the LDAP test fields while it runs, and names the user it tested', async () => {
    const test = deferred()
    api.adminTestLdap.mockReturnValue(test.promise)
    mount()
    await screen.findByRole('button', { name: 'Test connection' })
    const btn = screen.getByRole('button', { name: 'Test connection' })
    const inputs = [...btn.parentElement!.querySelectorAll('input')] as HTMLInputElement[]
    fireEvent.change(inputs[0], { target: { value: 'bob' } })
    fireEvent.change(inputs[1], { target: { value: 'pw' } })
    fireEvent.click(btn)
    await waitFor(() => expect(isLocked(inputs[0])).toBe(true))
    expect(isLocked(inputs[1])).toBe(true)
    await act(async () => test.resolve({ ok: true, stage: 'search', detail: '' }))
    // Edited after the test: the result still names the user it ran for.
    fireEvent.change(inputs[0], { target: { value: 'carol' } })
    expect(screen.getByText(/succeeded for user "bob"/)).toBeTruthy()
  })

  it('takes one login-user delete at a time', async () => {
    // A second click would only get a "not found" for the user just deleted.
    api.adminGetAuthSettings.mockResolvedValue({ ...AUTH, mode: 'basic' })
    api.adminListAuthUsers.mockResolvedValue([{ id: 1, username: 'alice' }, { id: 2, username: 'bob' }])
    api.adminDeleteAuthUser.mockReturnValue(new Promise(() => {}))
    vi.stubGlobal('confirm', vi.fn(() => true))
    mount()
    await screen.findByText('alice')
    const removes = screen.getAllByRole('button', { name: '×' }) as HTMLButtonElement[]
    fireEvent.click(removes[0])
    await waitFor(() => expect(removes.every((b) => b.disabled)).toBe(true))
    expect(api.adminDeleteAuthUser).toHaveBeenCalledTimes(1)
  })
})
