import React, { useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import {
  adminLogin,
  adminLoginMfa,
  getAuthState,
  isApiCodeError,
  isRateLimitError,
  isThirdPartyAnalyticsLoaded,
  setToken,
} from '../lib/api'
import { Button, FormField, Input, LockFieldset, Notice, Panel } from '../components/ui'
import { useI18n } from '../lib/i18n'
import { apiErrorMessageOr } from '../lib/admin/apiError'
import { clearSlideListState } from '../lib/admin/slideListState'
import { useAuthGate } from '../lib/useAuthGate'
import { loginUrlFor } from '../lib/nextPath'
import LanguageSwitcher from '../components/LanguageSwitcher'

export default function AdminLogin() {
  const nav = useNavigate()
  const loc = useLocation()
  const { t } = useI18n()
  // The dashboard is inside the app-access gate: under LDAP / Google sign-in
  // a visitor without an app session is sent to /login first (and back here
  // after), as on the public pages. The server enforces the same on the
  // login API (appAuthRequired below).
  useAuthGate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  // Set once the password was accepted for an account with two-factor
  // sign-in on: the form then asks for the code instead.
  const [mfaToken, setMfaToken] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [useRecovery, setUseRecovery] = useState(false)

  function enterDashboard(accessToken: string) {
    setToken(accessToken)
    // A sign-in starts the dashboard from its default list: an expired
    // session lands here without a logout, and the account signing in
    // need not be the one that left those filters.
    clearSlideListState()
    if (isThirdPartyAnalyticsLoaded()) {
      // gtag.js was loaded into this document while it was token-less (it
      // got here via SPA/history navigation from a public page). Start the
      // token-holding page from a fresh document: App skips GA on that
      // load because the token is already in sessionStorage.
      window.location.assign('/admin/dashboard')
      return
    }
    nav('/admin/dashboard')
    // Don't call setLoading(false) here — nav() unmounts this component
  }

  // The login API answered that the app sign-in comes first (an app session
  // that expired while this form was open, or a tab that skipped the check
  // above). Under LDAP / Google, go and sign in there; under Basic the next
  // page load raises the browser's dialog, so say so.
  async function onAppAuthRequired() {
    try {
      const s = await getAuthState()
      if (s.mode === 'ldap' || s.mode === 'google_oauth') {
        nav(loginUrlFor(loc), { replace: true })
        return
      }
    } catch { /* fall through to the message */ }
    backToPassword(t('admin.err.appAuthRequired'))
  }

  function isAppAuthRequired(e: unknown): boolean {
    return isApiCodeError(e) && e.code === 'admin.err.appAuthRequired'
  }

  function backToPassword(message = '') {
    setMfaToken(null)
    setCode('')
    setUseRecovery(false)
    setErr(message)
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setErr('')
    setLoading(true)
    try {
      const r = await adminLogin(username, password)
      if (r.mfa_required) {
        setPassword('')
        setMfaToken(r.mfa_token)
        setLoading(false)
        return
      }
      enterDashboard(r.access_token)
    } catch (e) {
      if (isAppAuthRequired(e)) {
        // Still locked while the auth mode is asked: its answer leaves the
        // page or resets the form.
        await onAppAuthRequired()
        setLoading(false)
        return
      }
      // 429 = per-IP throttle or per-account lockout (or the nginx rate
      // limit): retrying immediately won't help, so say so instead of
      // implying the credentials were wrong.
      setErr(isRateLimitError(e) ? t('admin.loginRateLimited') : t('admin.loginFailed'))
      setLoading(false)
    }
  }

  async function onSubmitCode(e: React.FormEvent) {
    e.preventDefault()
    if (!mfaToken) return
    if (!code.trim()) { setErr(t('adminMfa.codeRequired')); return }
    setErr('')
    setLoading(true)
    try {
      const r = await adminLoginMfa(mfaToken, code)
      if (r.recovery_codes_remaining !== undefined && r.recovery_codes_remaining <= 3) {
        window.alert(r.recovery_codes_remaining === 0
          ? t('adminMfa.recoveryNoneLeft')
          : t('adminMfa.recoveryLow', { n: r.recovery_codes_remaining }))
      }
      enterDashboard(r.access_token)
    } catch (e) {
      if (isAppAuthRequired(e)) {
        // As for the password step: locked until the auth mode is known.
        await onAppAuthRequired()
        setLoading(false)
        setCode('')
        return
      }
      setLoading(false)
      setCode('')
      if (isApiCodeError(e) && e.code === 'adminMfa.err.sessionExpired') {
        backToPassword(t('adminMfa.err.sessionExpired'))
        return
      }
      setErr(isRateLimitError(e) ? t('admin.loginRateLimited') : apiErrorMessageOr(e, t, 'admin.loginFailed'))
    }
  }

  return (
    <div style={{
      minHeight: '100vh',
      background: 'var(--color-bg)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: 'var(--space-6)',
    }}>
      <div style={{ width: '100%', maxWidth: 400 }}>
        <Panel padding="lg">
          <h2 style={{
            margin: 0,
            marginBottom: 'var(--space-5)',
            fontSize: 'var(--font-size-2xl)',
            fontWeight: 700,
            color: 'var(--color-text)',
            letterSpacing: '-0.01em',
          }}>
            {t('admin.loginTitle')}
          </h2>
          {mfaToken ? (
          <form onSubmit={onSubmitCode} style={{ display: 'grid', gap: 'var(--space-3)' }}>
            {/* Locked while the code is checked: a failure clears it, and
                "Start over" meanwhile would not stop a late success. */}
            <LockFieldset locked={loading} style={{ display: 'grid', gap: 'var(--space-3)' }}>
              <div style={{ fontWeight: 600 }}>{t('adminMfa.codeTitle')}</div>
              <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-muted)' }}>
                {useRecovery ? t('adminMfa.recoveryPrompt') : t('adminMfa.codePrompt')}
              </div>
              <FormField label={useRecovery ? t('adminMfa.recoveryLabel') : t('adminMfa.codeLabel')}>
                <Input
                  key={useRecovery ? 'recovery' : 'totp'}
                  type="text"
                  name={useRecovery ? 'recovery-code' : 'one-time-code'}
                  value={code}
                  onChange={e => setCode(e.target.value)}
                  inputMode={useRecovery ? 'text' : 'numeric'}
                  autoComplete={useRecovery ? 'off' : 'one-time-code'}
                  placeholder={useRecovery ? 'XXXXX-XXXXX' : '123456'}
                  maxLength={useRecovery ? 16 : 8}
                  autoFocus
                  required
                />
              </FormField>
              <Button type="submit" variant="primary" fullWidth loading={loading}>
                {t('adminMfa.verify')}
              </Button>
              <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8, fontSize: 'var(--font-size-sm)' }}>
                <button type="button" onClick={() => { setUseRecovery(v => !v); setCode(''); setErr('') }} style={LINK_BUTTON}>
                  {useRecovery ? t('adminMfa.useTotp') : t('adminMfa.useRecovery')}
                </button>
                <button type="button" onClick={() => backToPassword()} style={LINK_BUTTON}>
                  {t('adminMfa.startOver')}
                </button>
              </div>
            </LockFieldset>
            {err && <Notice error>{err}</Notice>}
          </form>
          ) : (
          <form onSubmit={onSubmit} style={{ display: 'grid', gap: 'var(--space-3)' }}>
            {/* Locked while the sign-in is out: its answer clears the password. */}
            <LockFieldset locked={loading} style={{ display: 'grid', gap: 'var(--space-3)' }}>
              <FormField label={t('admin.username')}>
                <Input
                  type="text"
                  name="username"
                  value={username}
                  onChange={e => setUsername(e.target.value)}
                  placeholder="admin"
                  autoComplete="username"
                  required
                />
              </FormField>
              <FormField label={t('admin.password')}>
                <Input
                  type="password"
                  name="password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  autoComplete="current-password"
                  required
                />
              </FormField>
              <Button type="submit" variant="primary" fullWidth loading={loading}>
                {t('admin.login')}
              </Button>
            </LockFieldset>
            {err && <Notice error>{err}</Notice>}
          </form>
          )}
        </Panel>
        <div style={{
          marginTop: 'var(--space-4)',
          textAlign: 'center',
          fontSize: 'var(--font-size-sm)',
        }}>
          {/* Full-page navigation, not an SPA <Link>: the public page this
              leads to may load GA, and a browser Back into this login form
              would otherwise bring that GA-loaded document to the admin login. */}
          <a href="/">{t('common.backToViewer')}</a>
          <div style={{ marginTop: 8 }}><LanguageSwitcher /></div>
        </div>
      </div>
    </div>
  )
}

const LINK_BUTTON: React.CSSProperties = {
  background: 'none',
  border: 'none',
  padding: 0,
  color: 'var(--color-brand-700)',
  cursor: 'pointer',
  font: 'inherit',
  textDecoration: 'underline',
}
