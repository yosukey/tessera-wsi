// @vitest-environment jsdom
import React from 'react'
import { BrowserRouter, Route, Routes } from 'react-router'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Browser history after leaving the Network or the Upload tab, with the real
// sections (and so their real navigation guards) under a BrowserRouter. The
// other sections and the slide list are stubbed; the API modules are the only
// I/O.
const api = vi.hoisted(() => ({
  adminMe: vi.fn(),
  adminListSlides: vi.fn(),
  adminListAllTours: vi.fn(),
  adminListMetadataOptions: vi.fn(),
  adminGetViewerSettings: vi.fn(),
  staffGetViewerOverlayDefaults: vi.fn(),
  clearToken: vi.fn(),
  clearAdminSession: vi.fn(),
  getRoleFromToken: vi.fn(),
  isAdminFromToken: vi.fn(),
  isManagerOrAboveFromToken: vi.fn(),
  adminGetNetworkSettings: vi.fn(),
  adminGetMyIp: vi.fn(),
  adminGetAuthSettings: vi.fn(),
  adminListAuthUsers: vi.fn(),
  adminUpdateNetworkSettings: vi.fn(),
  adminGetStorage: vi.fn(),
  adminUploadSlide: vi.fn(),
  adminCreateTour: vi.fn(),
  adminStageSlideImport: vi.fn(),
  pollTransferJob: vi.fn(),
}))
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return { ...actual, ...api }
})
// The upload queue imports these submodules directly.
vi.mock('../lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('../lib/api/admin')>('../lib/api/admin')
  return { ...actual, adminUploadSlide: api.adminUploadSlide }
})
vi.mock('../lib/api/auth', async () => {
  const actual = await vi.importActual<typeof import('../lib/api/auth')>('../lib/api/auth')
  return { ...actual, isStaffFromToken: () => true }
})
// The slide list's ways out of the dashboard: a tour's Edit link, New Tour.
vi.mock('../components/admin/SlideList', () => ({
  default: (p: {
    onLinkClick: (e: React.MouseEvent<HTMLAnchorElement>, to: string) => void
    onCreateTour: (slideId: number) => void
  }) => (
    <div>
      <a href="/admin/tours/10" onClick={(e) => p.onLinkClick(e, '/admin/tours/10')}>Edit tour</a>
      <button type="button" onClick={() => p.onCreateTour(1)}>New Tour</button>
    </div>
  ),
}))
vi.mock('../components/admin/AdminMetadataSection', () => ({ default: () => null }))
vi.mock('../components/admin/AdminViewerSettingsSection', () => ({ default: () => <div>SETTINGS-SECTION</div> }))
vi.mock('../components/admin/AdminBackupSection', () => ({ default: () => null }))
vi.mock('../components/admin/AdminExportHistorySection', () => ({ default: () => null }))
vi.mock('../components/admin/UrlShareControl', () => ({ default: () => null }))

import { I18nProvider } from '../lib/i18n'
import { UnauthorizedError } from '../lib/api'
import type { AuthSettings, NetworkSettings } from '../lib/api'
import AdminDashboard from './AdminDashboard'

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
  mode: 'none',
  ldap_server_uri: '',
  ldap_bind_dn: '',
  ldap_bind_password_configured: false,
  ldap_user_base_dn: '',
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

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} })
  vi.stubGlobal('confirm', () => true)
  api.isAdminFromToken.mockReturnValue(true)
  api.isManagerOrAboveFromToken.mockReturnValue(true)
  api.getRoleFromToken.mockReturnValue('admin')
  api.adminMe.mockResolvedValue({ id: 1, username: 'a', role: 'admin', role_permissions: {} })
  api.adminListSlides.mockResolvedValue([])
  api.adminListAllTours.mockResolvedValue([])
  api.adminListMetadataOptions.mockResolvedValue({ options: {} })
  api.adminGetViewerSettings.mockResolvedValue({})
  api.staffGetViewerOverlayDefaults.mockResolvedValue({
    show_zoom_indicator: false, show_scale_bar: true, show_metadata_panel: true,
  })
  api.adminGetNetworkSettings.mockResolvedValue(NET)
  api.adminGetMyIp.mockResolvedValue({ ip: '127.0.0.1', via_cloudflare_tunnel: false })
  api.adminGetAuthSettings.mockResolvedValue(AUTH)
  api.adminListAuthUsers.mockResolvedValue([])
  api.adminUpdateNetworkSettings.mockImplementation(async (p: Partial<NetworkSettings>) => ({
    ...NET, ...p, mdns_ok: true, mdns_msg: '',
  }))
  api.adminGetStorage.mockResolvedValue({ total_bytes: 0, free_bytes: 0 })
  api.adminUploadSlide.mockResolvedValue({ ok: true, slide_id: 9 })
  // The bundle's staging job: still running.
  api.pollTransferJob.mockReturnValue(new Promise(() => {}))
  sessionStorage.clear()
  // The page the admin came from, then the dashboard itself.
  window.history.replaceState(null, '', '/admin/prev')
  window.history.pushState(null, '', '/admin/dashboard')
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.resetAllMocks()
})

function mount() {
  return render(
    <BrowserRouter>
      <I18nProvider>
        <Routes>
          <Route path="/admin/dashboard" element={<AdminDashboard />} />
          <Route path="/admin/account" element={<div>ACCOUNT-PAGE</div>} />
          <Route path="/admin/tours/:id" element={<div>TOUR-PAGE</div>} />
          <Route path="/admin" element={<div>LOGIN-PAGE</div>} />
          <Route path="/admin/prev" element={<div>PREV-PAGE</div>} />
        </Routes>
      </I18nProvider>
    </BrowserRouter>,
  )
}

const tabButton = (name: string) => screen.queryAllByRole('button').find((b) => b.textContent === name)!
const goBack = () => act(async () => { window.history.back() })
const onGuardEntry = () => !!window.history.state?.wsiNavGuard

// Opens the Network tab and edits it, which makes its guard push its entry.
async function editNetworkTab() {
  mount()
  fireEvent.click(await waitFor(() => tabButton('Network')))
  fireEvent.click(await screen.findByLabelText('Announce service via mDNS'))
  await waitFor(() => expect(onGuardEntry()).toBe(true))
}

describe('AdminDashboard: leaving the Network tab leaves no extra Back press', () => {
  it('a confirmed header link', async () => {
    await editNetworkTab()
    fireEvent.click(screen.getByText('Account'))
    expect(await screen.findByText('ACCOUNT-PAGE')).toBeTruthy()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(onGuardEntry()).toBe(false)
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/prev'))
  })

  it("a confirmed slide list link to a tour's editor", async () => {
    const confirmSpy = vi.fn(() => true)
    vi.stubGlobal('confirm', confirmSpy)
    await editNetworkTab()
    fireEvent.click(screen.getByText('Edit tour'))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('TOUR-PAGE')).toBeTruthy()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(onGuardEntry()).toBe(false)
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/prev'))
  })

  it('a confirmed New Tour', async () => {
    const confirmSpy = vi.fn(() => true)
    vi.stubGlobal('confirm', confirmSpy)
    vi.stubGlobal('prompt', () => 'Tour')
    api.adminCreateTour.mockResolvedValue({ id: 11 })
    await editNetworkTab()
    fireEvent.click(screen.getByRole('button', { name: 'New Tour' }))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('TOUR-PAGE')).toBeTruthy()
    expect(window.location.pathname).toBe('/admin/tours/11')
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(onGuardEntry()).toBe(false)
  })

  it('a double click on a header link after the edits were saved steps back once', async () => {
    await editNetworkTab()
    fireEvent.click(screen.getByRole('button', { name: 'Save network settings' }))
    await screen.findByText('Saved')
    const back = vi.spyOn(window.history, 'back')
    fireEvent.click(screen.getByText('Account'))
    fireEvent.click(screen.getByText('Account'))
    expect(back).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('ACCOUNT-PAGE')).toBeTruthy()
    back.mockRestore()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(onGuardEntry()).toBe(false)
  })

  it('a header link after the edits were saved', async () => {
    await editNetworkTab()
    fireEvent.click(screen.getByRole('button', { name: 'Save network settings' }))
    await screen.findByText('Saved')
    fireEvent.click(screen.getByText('Account'))
    expect(await screen.findByText('ACCOUNT-PAGE')).toBeTruthy()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(onGuardEntry()).toBe(false)
  })

  it('a confirmed Logout', async () => {
    await editNetworkTab()
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
    expect(api.clearAdminSession).toHaveBeenCalledTimes(1)
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(onGuardEntry()).toBe(false)
  })

  it('a confirmed sidebar tab switch', async () => {
    await editNetworkTab()
    fireEvent.click(tabButton('Settings'))
    expect(await screen.findByText('SETTINGS-SECTION')).toBeTruthy()
    expect(window.location.pathname).toBe('/admin/dashboard')
    expect(onGuardEntry()).toBe(false)
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/prev'))
  })
})

// Runs one upload to completion on the Upload tab (the dashboard's first tab).
// The guard pushed its entry when the run started, and it stays in place
// after the run until the tab is left.
async function finishUpload() {
  const view = mount()
  const input = await waitFor(() => {
    const el = view.container.querySelector('input[type=file]')
    expect(el).not.toBeNull()
    return el as HTMLInputElement
  })
  fireEvent.change(input, { target: { files: [new File(['x'], 'a.svs')] } })
  fireEvent.click(await screen.findByRole('button', { name: 'Start upload' }))
  await waitFor(() => expect(api.adminUploadSlide).toHaveBeenCalledTimes(1))
  // Logout is disabled while the run lasts.
  await waitFor(() => expect((screen.getByRole('button', { name: 'Logout' }) as HTMLButtonElement).disabled).toBe(false))
  expect(onGuardEntry()).toBe(true)
}

describe('AdminDashboard: leaving the Upload tab after an upload leaves no extra Back press', () => {
  it('a header link', async () => {
    await finishUpload()
    fireEvent.click(screen.getByText('Account'))
    expect(await screen.findByText('ACCOUNT-PAGE')).toBeTruthy()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(onGuardEntry()).toBe(false)
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/prev'))
  })

  it('Logout', async () => {
    await finishUpload()
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(onGuardEntry()).toBe(false)
  })

  it("the uploader's own Log in again after the login expired mid-run", async () => {
    api.adminUploadSlide.mockRejectedValue(new UnauthorizedError(401))
    await finishUpload()
    fireEvent.click(await screen.findByRole('button', { name: 'Log in again' }))
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
    expect(api.clearToken).toHaveBeenCalled()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(onGuardEntry()).toBe(false)
  })

  it('a sidebar tab switch', async () => {
    await finishUpload()
    fireEvent.click(tabButton('Settings'))
    expect(await screen.findByText('SETTINGS-SECTION')).toBeTruthy()
    expect(window.location.pathname).toBe('/admin/dashboard')
    expect(onGuardEntry()).toBe(false)
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/prev'))
  })

  it("a slide list link to a tour's editor", async () => {
    await finishUpload()
    fireEvent.click(screen.getByText('Edit tour'))
    expect(await screen.findByText('TOUR-PAGE')).toBeTruthy()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(onGuardEntry()).toBe(false)
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/prev'))
  })

  it('New Tour', async () => {
    vi.stubGlobal('prompt', () => 'Tour')
    api.adminCreateTour.mockResolvedValue({ id: 12 })
    await finishUpload()
    fireEvent.click(screen.getByRole('button', { name: 'New Tour' }))
    expect(await screen.findByText('TOUR-PAGE')).toBeTruthy()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    expect(onGuardEntry()).toBe(false)
  })
})

describe('AdminDashboard: a rejected login', () => {
  it('replaces the dashboard with the login screen, so Back leaves (no loop after Logout)', async () => {
    api.adminMe.mockRejectedValue(new UnauthorizedError(401))
    mount()
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
    await goBack()
    expect(await screen.findByText('PREV-PAGE')).toBeTruthy()
    expect(window.location.pathname).toBe('/admin/prev')
  })
})

// The bundle upload on the Import / Export tab: an unmount cancels it while
// its body is being sent.
describe('AdminDashboard: leaving the Import / Export tab while a bundle is being sent', () => {
  async function sendBundle() {
    let signal: AbortSignal | undefined
    let sent: (() => void) | undefined
    let answer: ((v: { job_id: string }) => void) | undefined
    // Like the real one (keepAfterSent): an abort cancels it only while its
    // body is being sent.
    api.adminStageSlideImport.mockImplementation(
      (_f: File, _p: unknown, opts: { signal?: AbortSignal; onSent?: () => void }) => {
        signal = opts.signal
        let bodySent = false
        sent = () => { bodySent = true; opts.onSent?.() }
        return new Promise((resolve, reject) => {
          answer = resolve
          opts.signal?.addEventListener('abort', () => {
            if (!bodySent) reject(new DOMException('aborted', 'AbortError'))
          })
        })
      },
    )
    const view = mount()
    fireEvent.click(await waitFor(() => tabButton('Import / Export')))
    const input = await waitFor(() => {
      const el = view.container.querySelector('input[type=file][accept]')
      expect(el).not.toBeNull()
      return el as HTMLInputElement
    })
    fireEvent.change(input, { target: { files: [new File(['x'], 'bundle.tar.zst')] } })
    await waitFor(() => expect(onGuardEntry()).toBe(true))
    return { signal: () => signal, sent: () => sent!(), answer: (v: { job_id: string }) => answer!(v) }
  }

  it('asks first; declining keeps the upload going', async () => {
    const confirmSpy = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmSpy)
    const upload = await sendBundle()
    fireEvent.click(tabButton('Settings'))
    fireEvent.click(screen.getByText('Account'))
    expect(confirmSpy).toHaveBeenCalledTimes(2)
    expect(upload.signal()?.aborted).toBe(false)
    // Browser Back asks too (the section's guard).
    await goBack()
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(onGuardEntry()).toBe(true))
    expect(window.location.pathname).toBe('/admin/dashboard')
    expect(upload.signal()?.aborted).toBe(false)
  })

  it('a confirmed tab switch cancels it and leaves no extra Back press', async () => {
    vi.stubGlobal('confirm', () => true)
    const upload = await sendBundle()
    fireEvent.click(tabButton('Settings'))
    expect(await screen.findByText('SETTINGS-SECTION')).toBeTruthy()
    expect(upload.signal()?.aborted).toBe(true)
    expect(onGuardEntry()).toBe(false)
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/prev'))
  })

  it('asks before a reload until the server has answered, not once the body is sent', async () => {
    const confirmSpy = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmSpy)
    const upload = await sendBundle()
    const reloadAsks = () => !window.dispatchEvent(new Event('beforeunload', { cancelable: true }))
    expect(reloadAsks()).toBe(true)
    act(() => upload.sent())
    // Sent: leaving the tab no longer cancels it, so nothing is asked...
    fireEvent.click(tabButton('Settings'))
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(await screen.findByText('SETTINGS-SECTION')).toBeTruthy()
    // ...and a section mounted again follows it; a reload would still lose
    // the job id the answer carries.
    fireEvent.click(tabButton('Import / Export'))
    await waitFor(() => expect(reloadAsks()).toBe(true))
    act(() => upload.answer({ job_id: 'stage-job' }))
    await waitFor(() => expect(reloadAsks()).toBe(false))
  })
})
