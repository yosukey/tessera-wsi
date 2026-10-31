import type { MouseEvent as ReactMouseEvent } from 'react'
import { STORAGE_KEYS } from '../storageKeys'
import {
  UnauthorizedError,
  adminFetch,
  adminGet,
  adminSend,
  appendTextOrClear,
  authHeaders,
  getToken,
  throwApi,
} from './shared'

// getToken lives in shared.ts (the request helpers there need it); it is
// re-exported so the token's read sits beside its write and clear here.
export { getToken }

// The admin JWT is stored in sessionStorage rather than localStorage so it
// is scoped to a single browser tab and cleared when that tab closes. This
// shrinks the window in which an XSS payload (e.g. delivered via a
// compromised third-party script on a public viewer page) could read it
// from a separate tab.
const ADMIN_TOKEN_KEY = STORAGE_KEYS.adminToken

/**
 * A Ctrl/Cmd/Shift-click or middle-click opens the link with no opener in
 * Chromium, whatever its ``rel``, so the new tab could not reach the token and
 * a draft would 404 there. Open those through window.open instead, which
 * keeps the opener (the tab comes up in the foreground rather than behind).
 * A plain left click is left to the link itself.
 */
function openAdminPreviewKeepingOpener(e: ReactMouseEvent<HTMLAnchorElement>): void {
  const modified = e.button === 0 && (e.ctrlKey || e.metaKey || e.shiftKey)
  if (!modified && e.button !== 1) return
  e.preventDefault()
  // No "noreferrer" feature: it would imply noopener. The Referer is a
  // same-origin dashboard path, and the adopted token keeps GA off that tab.
  window.open(e.currentTarget.href, '_blank')
}

/**
 * Props for a dashboard link that opens an admin preview (``?from=admin``) in
 * a new tab. sessionStorage is per tab, so the new tab starts without the
 * token it needs to show a draft or start live sync; ``rel="opener"`` keeps
 * the new tab's ``window.opener`` (``target="_blank"`` implies noopener
 * otherwise), and adoptOpenerAdminToken below reads the token straight out
 * of the opening tab's sessionStorage. Nothing is written anywhere another
 * tab could read: a handover through localStorage would have its ``storage``
 * event deliver it to every open same-origin tab, public pages and their
 * third-party scripts included. ``referrerPolicy`` keeps the Referer off as
 * ``noreferrer`` would (``rel="noreferrer"`` would imply noopener again).
 */
export const adminPreviewLinkProps = {
  target: '_blank',
  rel: 'opener',
  referrerPolicy: 'no-referrer',
  onClick: openAdminPreviewKeepingOpener,
  onAuxClick: openAdminPreviewKeepingOpener,
} as const

// On first load, a tab the dashboard opened through adminPreviewLinkProps
// copies the token from its opener's sessionStorage, then lets go of the
// opener: a public viewer page has no further use for a handle on the
// dashboard tab. Runs at module load, before App could have injected GA.
;(function adoptOpenerAdminToken() {
  try {
    // Remove any admin token left in localStorage: it is only ever kept in
    // sessionStorage.
    localStorage.removeItem(ADMIN_TOKEN_KEY)
  } catch {
    // localStorage may be unavailable (private mode, etc.).
  }
  try {
    if (new URLSearchParams(window.location.search).get('from') !== 'admin') return
    const opener = window.opener as Window | null
    if (!opener) return
    try {
      if (!sessionStorage.getItem(ADMIN_TOKEN_KEY)) {
        // Throws (SecurityError) when the opener is, or has since navigated
        // to, another origin.
        const token = opener.sessionStorage.getItem(ADMIN_TOKEN_KEY)
        if (token) sessionStorage.setItem(ADMIN_TOKEN_KEY, token)
      }
    } finally {
      window.opener = null
    }
  } catch {
    // No token adopted: the tab falls back to its non-admin code path.
  }
})()

export function setToken(t: string) {
  try {
    sessionStorage.setItem(ADMIN_TOKEN_KEY, t)
  } catch {
    // best-effort; the request that triggered this will fail and surface
    // the error normally if storage is unavailable.
  }
}

/**
 * True when App's useGoogleAnalytics has already injected gtag.js into this
 * document. App only skips GA while a token is present, so a document that
 * loaded GA while token-less (e.g. /admin → SPA link to a public page →
 * browser Back → log in) would otherwise end up holding the JWT next to a
 * third-party script. Login checks this and, when true, enters the dashboard
 * with a full page load instead of an SPA navigation, so the token-holding
 * page starts from a fresh document with no GA in it.
 */
export function isThirdPartyAnalyticsLoaded(): boolean {
  try {
    return !!document.getElementById('ga-script')
      || typeof (window as unknown as { gtag?: unknown }).gtag === 'function'
  } catch {
    return false
  }
}

export function clearToken() {
  try {
    sessionStorage.removeItem(ADMIN_TOKEN_KEY)
    localStorage.removeItem(ADMIN_TOKEN_KEY)
    // The dashboard's saved search / filters belong to the admin signing
    // out, not to whoever signs in next on this tab.
    sessionStorage.removeItem(STORAGE_KEYS.adminSlideListState)
  } catch {
    // ignore
  }
}

// Which dashboard sign-in the page is in. It changes at an explicit sign-out
// (clearAdminSession) and at nothing else. Work that was under way when the
// admin signed out — an upload waiting for the server's answer, a job being
// started — finishes after it and would write its pointer into sessionStorage
// AFTER clearAdminSession removed them, handing the next account to sign in on
// this tab another admin's staged restore or import. Such work reads the epoch
// when it starts and drops its write if the epoch has moved on.
let adminSessionEpoch = 0

export function currentAdminSessionEpoch(): number {
  return adminSessionEpoch
}

/** True while ``epoch`` (from currentAdminSessionEpoch) is still the sign-in. */
export function adminSessionIsCurrent(epoch: number): boolean {
  return epoch === adminSessionEpoch
}

// An explicit sign-out of the dashboard: the token, plus the pointers to
// server-side work the signed-out admin had under way (a backup or restore
// staging job, a staged slide-import bundle). They live in sessionStorage so a
// tab switch or reload re-attaches to the job; left behind, the NEXT account
// to sign in on this tab would be shown another admin's staged restore or
// import. Deliberately not part of clearToken(): the "session ended during a
// restore" path clears the token too, and that admin signs straight back in
// to the very job those keys point at.
export function clearAdminSession() {
  adminSessionEpoch += 1
  clearToken()
  try {
    for (const key of [
      STORAGE_KEYS.backupJobId,
      STORAGE_KEYS.restoreStageJobId,
      STORAGE_KEYS.importKeptStageId,
      STORAGE_KEYS.importKeptStageFallbackId,
      STORAGE_KEYS.importStageJobId,
    ]) {
      sessionStorage.removeItem(key)
    }
  } catch {
    // ignore
  }
}

// Client-side admin check based on the JWT payload. The server still
// enforces admin on every privileged endpoint, so this is only used to
// decide whether to surface admin-only UI affordances (e.g. the debug
// toggle in the viewer).
function decodeJwtPayload(token: string): Record<string, unknown> | null {
  const parts = token.split('.')
  if (parts.length !== 3) return null
  // JWT uses base64url with stripped padding. atob() needs standard base64
  // with padding restored, otherwise short payloads can throw
  // InvalidCharacterError and silently lose admin status until next login.
  let b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
  const pad = b64.length % 4
  if (pad === 2) b64 += '=='
  else if (pad === 3) b64 += '='
  else if (pad === 1) return null  // malformed
  try {
    return JSON.parse(atob(b64))
  } catch {
    return null
  }
}

export type Role = 'admin' | 'manager' | 'instructor'

// True when this tab's dashboard session came from signing in with the last
// recovery code (the token's amr claim): it may replace the codes without a
// code. Only decides what the account panel offers; the server checks it.
export function isLastRecoveryCodeSession(): boolean {
  const token = getToken()
  return !!token && decodeJwtPayload(token)?.amr === 'recovery'
}

export function getRoleFromToken(): Role | null {
  const token = getToken()
  if (!token) return null
  const payload = decodeJwtPayload(token)
  if (!payload) return null
  const role = (payload as { role?: unknown }).role
  if (role !== 'admin' && role !== 'manager' && role !== 'instructor') return null
  const exp = (payload as { exp?: unknown }).exp
  if (typeof exp === 'number' && exp < Math.floor(Date.now() / 1000)) return null
  return role
}

export function isAdminFromToken(): boolean {
  return getRoleFromToken() === 'admin'
}

export function isManagerOrAboveFromToken(): boolean {
  const r = getRoleFromToken()
  return r === 'admin' || r === 'manager'
}

export function isStaffFromToken(): boolean {
  return getRoleFromToken() !== null
}

// The password step answers with the staff token, or — for an account with
// two-factor sign-in on — with a short-lived mfa_token that adminLoginMfa
// trades, together with a code, for the staff token.
export type AdminLoginResult =
  | { access_token: string; mfa_required?: undefined }
  | { mfa_required: true; mfa_token: string; access_token?: undefined }

export async function adminLogin(username: string, password: string): Promise<AdminLoginResult> {
  const r = await fetch('/api/admin/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  // Surface server detail (Invalid credentials / lockout 429 / etc.) instead
  // of a generic "login failed" so the login form can show a useful message.
  if (!r.ok) await throwApi(r, 'login failed')
  return r.json() as Promise<AdminLoginResult>
}

// `code` is a 6-digit authenticator code or a recovery code (XXXXX-XXXXX).
// recovery_codes_remaining is present when a recovery code was used.
export async function adminLoginMfa(
  mfaToken: string,
  code: string,
): Promise<{ access_token: string; recovery_codes_remaining?: number }> {
  const r = await fetch('/api/admin/login/mfa', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mfa_token: mfaToken, code }),
  })
  if (!r.ok) await throwApi(r, 'login failed')
  return r.json()
}

export type RolePermissions = {
  manager_can_view_analytics: boolean
  manager_can_view_server: boolean
  instructor_can_view_analytics: boolean
  instructor_can_view_server: boolean
}

export type MfaStatus = {
  enabled: boolean
  enabled_at: string | null
  recovery_codes_remaining: number
  // False when the server has no WSI_MFA_KEY: two-factor cannot be turned on.
  available: boolean
}

export type AdminMe = {
  id: number
  username: string
  role: Role
  role_permissions: RolePermissions
  mfa?: MfaStatus
}

export async function adminMe(opts: { signal?: AbortSignal } = {}): Promise<AdminMe> {
  const r = await fetch('/api/admin/me', { headers: authHeaders(), signal: opts.signal })
  // Distinguish "your token is no good" from "the request failed": the
  // dashboard bounces to the login screen on the former only. Without this an
  // admin holding a valid token would be evicted by any transient failure of this
  // one call — an nginx 429 (the dashboard fires four requests on mount into a
  // 120r/m burst=40 limit), a momentary 500, a network blip — losing whatever
  // context they had open.
  if (r.status === 401 || r.status === 403) throw new UnauthorizedError(r.status)
  if (!r.ok) await throwApi(r, 'me failed')
  return r.json() as Promise<AdminMe>
}

export async function adminUpdateMe(payload: {
  current_password: string
  username?: string
  new_password?: string
}) {
  const r = await adminFetch('/api/admin/me', 'update failed', { method: 'PATCH', body: payload })
  const data = (await r.json()) as {
    ok: boolean
    id: number
    username: string
    role: Role
    // Present only when the password was changed: the server bumped the
    // account's token_version (revoking every prior token, including the
    // one just sent) and minted this replacement. Swap it in so the
    // current session keeps working instead of 401'ing on the next call.
    access_token?: string
  }
  if (data.access_token) setToken(data.access_token)
  return data
}

// Ends every session of the signed-in account on the server (it bumps the
// account's token_version), this one included. The caller then forgets its own
// token and returns to the sign-in screen.
export async function adminSignOutEverywhere() {
  await adminFetch('/api/admin/me/sign-out-everywhere', 'sign out everywhere failed', { method: 'POST' })
}

// ===== Two-factor sign-in (the signed-in account's own) =====
export type MfaSetup = {
  secret: string
  otpauth_uri: string
  issuer: string
  account: string
  setup_token: string
  expires_in: number
}

export async function adminMfaSetup(currentPassword: string): Promise<MfaSetup> {
  return adminSend('POST', '/api/admin/me/mfa/setup', { current_password: currentPassword }, 'mfa setup failed')
}

// Turning two-factor on or off ends the account's other sessions; the
// response carries a replacement token for this one, swapped in here.
export async function adminMfaConfirm(
  setupToken: string,
  code: string,
): Promise<{ recovery_codes: string[]; mfa: MfaStatus }> {
  const data = await adminSend<{ recovery_codes: string[]; access_token: string; mfa: MfaStatus }>(
    'POST', '/api/admin/me/mfa/confirm', { setup_token: setupToken, code }, 'mfa confirm failed',
  )
  setToken(data.access_token)
  return data
}

export async function adminMfaDisable(currentPassword: string, code: string): Promise<{ mfa: MfaStatus }> {
  const data = await adminSend<{ access_token: string; mfa: MfaStatus }>(
    'POST', '/api/admin/me/mfa/disable', { current_password: currentPassword, code }, 'mfa disable failed',
  )
  setToken(data.access_token)
  return data
}

// New codes end the account's other sessions too (one may have been opened
// with a leaked code); the replacement token for this one is swapped in.
export async function adminMfaRegenerateRecoveryCodes(
  currentPassword: string,
  code: string,
): Promise<{ recovery_codes: string[]; mfa: MfaStatus }> {
  const data = await adminSend<{ recovery_codes: string[]; access_token: string; mfa: MfaStatus }>(
    'POST', '/api/admin/me/mfa/recovery-codes', { current_password: currentPassword, code }, 'mfa recovery codes failed',
  )
  setToken(data.access_token)
  return data
}

// ===== App-access authentication =====
export type AuthMode = 'none' | 'basic' | 'ldap' | 'google_oauth'

// How far the "skip auth for direct LAN access" exemption reaches:
//   'off' — never; 'ip' — bare private-IP literal Host only (direct IP typed
//   into the address bar); 'lan' — any LAN-direct access (bare IP or vhost).
export type LanDirectNoauthMode = 'off' | 'ip' | 'lan'

export type AuthSettings = {
  mode: AuthMode
  ldap_server_uri: string
  ldap_bind_dn: string
  ldap_bind_password_configured: boolean
  ldap_user_base_dn: string
  ldap_user_filter: string
  ldap_use_starttls: boolean
  google_client_id: string
  google_client_secret_configured: boolean
  google_allowed_domains: string
  google_allowed_emails: string
  session_ttl_seconds: number
  login_message: string
  login_button_label: string
  lan_direct_noauth_mode: LanDirectNoauthMode
}

export type AuthUser = { id: number; username: string }

export type AuthState = {
  mode: AuthMode
  // True with an app-access session, a verified Cloudflare Access identity,
  // or (gated modes) the staff token this tab holds: staff are never asked
  // for a second, app-access login.
  authenticated: boolean
  // True only with an app-access session cookie, the one thing
  // appLogout() ends; never for the staff token or a Cloudflare Access
  // identity alone.
  app_session: boolean
  // Operator overrides for the login screen; empty -> use the per-mode
  // built-in i18n defaults.
  login_message?: string
  login_button_label?: string
}

export async function adminGetAuthSettings(opts: { signal?: AbortSignal } = {}): Promise<AuthSettings> {
  return adminGet('/api/admin/auth/settings', 'auth settings failed', { signal: opts.signal })
}

// Text fields whose empty value means "clear it" (the server has a
// clear_<field> flag for each). The two secrets are deliberately absent: an
// empty secret input means "keep the stored one", and clearing those goes
// through the explicit clear_ldap_bind_password / clear_google_client_secret.
const AUTH_CLEARABLE_TEXT_FIELDS = new Set([
  'ldap_server_uri',
  'ldap_bind_dn',
  'ldap_user_base_dn',
  'ldap_user_filter',
  'google_client_id',
  'google_allowed_domains',
  'google_allowed_emails',
  'login_message',
  'login_button_label',
])

export async function adminUpdateAuthSettings(payload: Partial<{
  mode: AuthMode
  ldap_server_uri: string
  ldap_bind_dn: string
  ldap_bind_password: string
  clear_ldap_bind_password: boolean
  ldap_user_base_dn: string
  ldap_user_filter: string
  ldap_use_starttls: boolean
  google_client_id: string
  google_client_secret: string
  clear_google_client_secret: boolean
  google_allowed_domains: string
  google_allowed_emails: string
  session_ttl_seconds: number
  login_message: string
  login_button_label: string
  lan_direct_noauth_mode: LanDirectNoauthMode
  // Ends every app-access (visitor) login now; see "Sign everyone out".
  revoke_app_sessions: boolean
}>): Promise<AuthSettings & { warning?: string }> {
  const form = new FormData()
  for (const [k, v] of Object.entries(payload)) {
    if (v === undefined) continue
    if (typeof v === 'boolean') form.append(k, v ? '1' : '0')
    else if (AUTH_CLEARABLE_TEXT_FIELDS.has(k)) appendTextOrClear(form, k, String(v))
    else form.append(k, String(v))
  }
  return adminSend('PATCH', '/api/admin/auth/settings', form, 'update auth settings failed')
}

export type LdapTestStage =
  | 'config'
  | 'import'
  | 'connect'
  | 'starttls'
  | 'bind'
  | 'search'
  | 'not_found'
  | 'user_bind'
  | 'ok'

export type LdapTestResult = { ok: boolean; stage: LdapTestStage; detail: string }

export async function adminTestLdap(username = '', password = ''): Promise<LdapTestResult> {
  const form = new FormData()
  if (username) form.append('username', username)
  if (password) form.append('password', password)
  return adminSend('POST', '/api/admin/auth/ldap/test', form, 'ldap test failed')
}

export async function adminListAuthUsers(opts: { signal?: AbortSignal } = {}): Promise<AuthUser[]> {
  return adminGet('/api/admin/auth/users', 'list auth users failed', { signal: opts.signal })
}

export async function adminAddAuthUser(username: string, password: string): Promise<AuthUser> {
  const form = new FormData()
  form.append('username', username)
  form.append('password', password)
  return adminSend('POST', '/api/admin/auth/users', form, 'add user failed')
}

export async function adminUpdateAuthUserPassword(id: number, password: string): Promise<void> {
  const form = new FormData()
  form.append('password', password)
  await adminFetch(`/api/admin/auth/users/${id}`, 'update password failed', { method: 'PATCH', body: form })
}

export async function adminDeleteAuthUser(id: number): Promise<void> {
  await adminFetch(`/api/admin/auth/users/${id}`, 'delete user failed', { method: 'DELETE' })
}

export async function getAuthState(): Promise<AuthState> {
  // Send the dashboard's staff token when this tab holds one: the server lets
  // staff through without an app-access session, and reports so here.
  const r = await fetch('/api/auth/state', {
    credentials: 'same-origin',
    headers: authHeaders(),
  })
  if (!r.ok) await throwApi(r, 'auth state failed')
  return r.json()
}

export async function appLogin(username: string, password: string): Promise<void> {
  const form = new FormData()
  form.append('username', username)
  form.append('password', password)
  const r = await fetch('/api/auth/login', {
    method: 'POST',
    body: form,
    credentials: 'same-origin',
  })
  if (!r.ok) await throwApi(r, 'login failed')
}

// Ends the app-access session. Rejects unless the session is known to be
// gone: only the app's answer carries the cookie deletion, so after a 429
// (nginx's API rate limit) or a 5xx (app or gateway down) the cookie is still
// set, and treating that as signed out would send the visitor to /login, which
// would bounce them straight back to the page, still signed in. The endpoint
// answers 200 whether or not the session was still valid (/api/auth/* is
// outside the session gate, and nothing in front of it answers 401), so any
// other status — a 401 from some future proxy included — is a failure: it
// cannot have cleared the cookie either.
export async function appLogout(): Promise<void> {
  const r = await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' })
  if (!r.ok) await throwApi(r, 'logout failed')
}
