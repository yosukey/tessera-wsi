import React, { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { AdminMe, adminMe, adminSignOutEverywhere, adminUpdateMe, clearAdminSession, isAbortError, isUnauthorizedError } from '../lib/api'
import { Badge, Button, FormField, Input, LockFieldset, Notice, Panel } from '../components/ui'
import { logger } from '../lib/logger'
import { useI18n } from '../lib/i18n'
import { apiErrorMessage, localizedErrorDetail, withErrorDetail } from '../lib/admin/apiError'
import LanguageSwitcher from '../components/LanguageSwitcher'
import AdminUsersSection from '../components/admin/AdminUsersSection'
import AccountMfaSection from '../components/admin/AccountMfaSection'
import { useInFlight } from '../hooks/useInFlight'

export default function AdminAccount() {
  const nav = useNavigate()
  const { t } = useI18n()
  const [me, setMe] = useState<AdminMe | null>(null)
  // The error of a failed /me load, so the page says why the two-factor and
  // user-management panels are missing (a sign-in that is no longer valid
  // goes to the login page instead). Worded at render, in the language shown.
  const [meLoadError, setMeLoadError] = useState<unknown>(null)

  const [username, setUsername] = useState('')
  const [usernameCurrentPw, setUsernameCurrentPw] = useState('')
  const [usernameMsg, setUsernameMsg] = useState('')
  const [usernameError, setUsernameError] = useState(false)
  // Each form is locked while its request is in flight: a success clears the
  // password fields, which would wipe what was typed meanwhile (and report a
  // change as done for values that were never sent), and a second submit
  // would send the request again.
  const usernameRequest = useInFlight()
  const usernameBusy = usernameRequest.busy

  const [pwCurrent, setPwCurrent] = useState('')
  const [pwNew, setPwNew] = useState('')
  const [pwConfirm, setPwConfirm] = useState('')
  const [pwMsg, setPwMsg] = useState('')
  const [pwError, setPwError] = useState(false)
  const pwRequest = useInFlight()
  const signOutRequest = useInFlight()
  const pwBusy = pwRequest.busy

  const [sessionsMsg, setSessionsMsg] = useState('')

  useEffect(() => {
    const ctrl = new AbortController()
    adminMe({ signal: ctrl.signal })
      .then(u => {
        setMe(u)
        // Don't clobber an in-progress edit. If the admin started typing a
        // new username before adminMe resolved (slow network, cold cache),
        // overwriting their input with the persisted value reads as the
        // form mysteriously reverting. Seed only when the field is still
        // its initial empty value.
        setUsername(prev => prev ? prev : u.username)
      })
      .catch((err) => {
        if (isAbortError(err)) return
        // Only a genuine auth rejection returns to the login screen; a
        // transient failure (429 / 500 / network blip) must not evict an admin
        // holding a valid token. Mirrors AdminDashboard (replacing this entry).
        if (isUnauthorizedError(err)) { nav('/admin', { replace: true }); return }
        logger.error('adminMe failed:', err)
        setMeLoadError(err)
      })
    return () => ctrl.abort()
  }, [])

  async function onUpdateUsername(e: React.FormEvent) {
    e.preventDefault()
    if (usernameBusy) return
    setUsernameMsg('')
    setUsernameError(false)
    const trimmed = username.trim()
    if (!trimmed) {
      setUsernameMsg(t('admin.usernameRequired')); setUsernameError(true); return
    }
    if (!usernameCurrentPw) {
      setUsernameMsg(t('admin.passwordRequired')); setUsernameError(true); return
    }
    await usernameRequest.run(async () => {
      try {
        const r = await adminUpdateMe({ current_password: usernameCurrentPw, username: trimmed })
        // adminUpdateMe omits role_permissions and mfa, so merge the updated username
        // into the existing record rather than replacing it wholesale.
        setMe(prev => (prev ? { ...prev, username: r.username, role: r.role } : prev))
        setUsernameCurrentPw('')
        setUsernameMsg(t('admin.usernameUpdated'))
      } catch (err: any) {
        setUsernameMsg(apiErrorMessage(err, t) || t('admin.updateFailed'))
        setUsernameError(true)
      }
    })
  }

  async function onChangePassword(e: React.FormEvent) {
    e.preventDefault()
    if (pwBusy) return
    setPwMsg('')
    setPwError(false)
    if (!pwCurrent) {
      setPwMsg(t('admin.passwordRequired')); setPwError(true); return
    }
    if (pwNew.length < 8) {
      setPwMsg(t('admin.passwordTooShort')); setPwError(true); return
    }
    if (pwNew !== pwConfirm) {
      setPwMsg(t('admin.passwordMismatch')); setPwError(true); return
    }
    await pwRequest.run(async () => {
      try {
        await adminUpdateMe({ current_password: pwCurrent, new_password: pwNew })
        setPwCurrent('')
        setPwNew('')
        setPwConfirm('')
        setPwMsg(t('admin.passwordUpdated'))
      } catch (err: any) {
        setPwMsg(apiErrorMessage(err, t) || t('admin.updateFailed'))
        setPwError(true)
      }
    })
  }

  function onLogout() {
    if (!window.confirm(t('admin.logoutConfirm'))) return
    clearAdminSession()
    nav('/admin')
  }

  async function onSignOutEverywhere() {
    if (!window.confirm(t('admin.signOutEverywhereConfirm'))) return
    await signOutRequest.run(async () => {
      setSessionsMsg('')
      try {
        await adminSignOutEverywhere()
      } catch (err: any) {
        // A token the server no longer accepts is already signed out everywhere.
        if (!isUnauthorizedError(err)) {
          setSessionsMsg(apiErrorMessage(err, t) || t('admin.signOutEverywhereFailed'))
          return
        }
      }
      clearAdminSession()
      nav('/admin')
    })
  }

  return (
    <div style={{
      minHeight: '100vh',
      background: 'var(--color-bg)',
      padding: 'var(--space-6) var(--space-4)',
    }}>
      <div style={{ width: '100%', maxWidth: 960, margin: '0 auto' }}>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 'var(--space-3)',
          marginBottom: 'var(--space-4)',
        }}>
          <h2 style={{
            margin: 0,
            fontSize: 'var(--font-size-2xl)',
            fontWeight: 700,
            letterSpacing: '-0.01em',
          }}>
            {t('admin.accountTitle')}
          </h2>
          <div style={{ flex: 1 }} />
          <Link
            to="/admin/dashboard"
            style={{ fontSize: 'var(--font-size-sm)' }}
          >
            {t('admin.dashboard')}
          </Link>
          <LanguageSwitcher />
          <Button size="sm" variant="ghost" onClick={onLogout}>{t('admin.logout')}</Button>
        </div>

        <div style={{
          fontSize: 'var(--font-size-sm)',
          color: 'var(--color-text-muted)',
          marginBottom: 'var(--space-5)',
          display: 'flex',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 'var(--space-2)',
        }}>
          {t('admin.currentUser')}
          <strong style={{ color: 'var(--color-text)' }}>{me?.username || '...'}</strong>
          {me?.role && <RoleBadge role={me.role} />}
        </div>

        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))',
          gap: 'var(--space-4)',
          marginBottom: 'var(--space-5)',
          alignItems: 'start',
        }}>
          <Panel padding="lg">
            <SectionHeader
              title={t('admin.changeUsername')}
              description={t('admin.changeUsernameDesc')}
            />
            <form onSubmit={onUpdateUsername} style={{ display: 'grid', gap: 'var(--space-3)' }}>
              <LockFieldset locked={usernameBusy} style={{ display: 'grid', gap: 'var(--space-3)' }}>
              <FormField label={t('admin.newUsername')}>
                <Input
                  type="text"
                  name="username"
                  value={username}
                  onChange={e => setUsername(e.target.value)}
                  placeholder="admin"
                  autoComplete="username"
                />
              </FormField>
              <FormField label={t('admin.currentPasswordConfirm')}>
                <Input
                  type="password"
                  name="current-password"
                  value={usernameCurrentPw}
                  onChange={e => setUsernameCurrentPw(e.target.value)}
                  autoComplete="current-password"
                />
              </FormField>
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <Button type="submit" variant="primary">{t('admin.updateUsername')}</Button>
              </div>
              </LockFieldset>
              {usernameMsg && <Notice error={usernameError}>{usernameMsg}</Notice>}
            </form>
          </Panel>

          <Panel padding="lg">
            <SectionHeader
              title={t('admin.changePassword')}
              description={t('admin.changePasswordDesc')}
            />
            <form onSubmit={onChangePassword} style={{ display: 'grid', gap: 'var(--space-3)' }}>
              <LockFieldset locked={pwBusy} style={{ display: 'grid', gap: 'var(--space-3)' }}>
              <FormField label={t('admin.currentPassword')}>
                <Input
                  type="password"
                  name="current-password"
                  value={pwCurrent}
                  onChange={e => setPwCurrent(e.target.value)}
                  autoComplete="current-password"
                />
              </FormField>
              <FormField label={t('admin.newPassword')} description={t('admin.passwordHint')}>
                <Input
                  type="password"
                  name="new-password"
                  value={pwNew}
                  onChange={e => setPwNew(e.target.value)}
                  autoComplete="new-password"
                />
              </FormField>
              <FormField label={t('admin.newPasswordConfirm')}>
                <Input
                  type="password"
                  name="confirm-new-password"
                  value={pwConfirm}
                  onChange={e => setPwConfirm(e.target.value)}
                  autoComplete="new-password"
                />
              </FormField>
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <Button type="submit" variant="primary">{t('admin.updatePassword')}</Button>
              </div>
              </LockFieldset>
              {pwMsg && <Notice error={pwError}>{pwMsg}</Notice>}
            </form>
          </Panel>

          <Panel padding="lg">
            <SectionHeader
              title={t('admin.sessionsTitle')}
              description={t('admin.sessionsDesc')}
            />
            <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <Button variant="danger" onClick={onSignOutEverywhere} disabled={signOutRequest.busy}>{t('admin.signOutEverywhere')}</Button>
              </div>
              {sessionsMsg && <Notice error>{sessionsMsg}</Notice>}
            </div>
          </Panel>

          {!me && meLoadError !== null && (
            <Panel padding="lg">
              <Notice error>
                <div>{withErrorDetail(t('admin.accountLoadFailed'), localizedErrorDetail(meLoadError, t))}</div>
                <div>{t('admin.accountLoadFailedEffect')} {t('common.reloadPageToRetry')}</div>
              </Notice>
            </Panel>
          )}
          {me && (
            <Panel padding="lg">
              <SectionHeader
                title={t('adminMfa.sectionTitle')}
                description={t('adminMfa.sectionDesc')}
              />
              <AccountMfaSection
                username={me.username}
                mfa={me.mfa}
                onChange={mfa => setMe(prev => (prev ? { ...prev, mfa } : prev))}
              />
            </Panel>
          )}
        </div>

        {me && me.role === 'admin' && (
          <Panel padding="lg">
            <AdminUsersSection me={me} />
          </Panel>
        )}
      </div>
    </div>
  )
}

function SectionHeader({ title, description }: { title: string; description: string }) {
  return (
    <div style={{ marginBottom: 'var(--space-4)' }}>
      <h3 style={{
        margin: 0,
        marginBottom: 'var(--space-1)',
        fontSize: 'var(--font-size-lg)',
        fontWeight: 600,
      }}>
        {title}
      </h3>
      <p style={{
        margin: 0,
        fontSize: 'var(--font-size-sm)',
        color: 'var(--color-text-muted)',
      }}>
        {description}
      </p>
    </div>
  )
}

function RoleBadge({ role }: { role: 'admin' | 'manager' | 'instructor' }) {
  const { t } = useI18n()
  const tone: 'brand' | 'success' | 'info' =
    role === 'admin' ? 'brand' : role === 'manager' ? 'success' : 'info'
  return <Badge tone={tone}>{t(`roles.${role}`)}</Badge>
}
