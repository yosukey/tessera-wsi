import React, { useEffect, useState } from 'react'
import {
  AuthMode,
  AuthSettings,
  AuthUser,
  LdapTestResult,
  adminAddAuthUser,
  adminDeleteAuthUser,
  adminGetAuthSettings,
  adminListAuthUsers,
  adminTestLdap,
  adminUpdateAuthSettings,
  adminUpdateAuthUserPassword,
  isAbortError,
} from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { apiErrorMessage, failureMessage } from '../../lib/admin/apiError'
import { logger } from '../../lib/logger'
import { useInFlight } from '../../hooks/useInFlight'
import { LockFieldset } from '../ui'
import {
  DIRTY_SAVE_STYLE,
  FIELD_LABEL_STYLE,
  SECTION_STYLE,
  SECTION_TITLE_STYLE,
  SaveResult,
  UnsavedChangesChip,
} from './networkAuthUi'

// Localized text for the save response's `warning` (the mode's prerequisites
// are missing; see _auth_mode_readiness_warning in routers/admin_settings.py),
// keyed by the mode it refers to. 'none' never warns.
const AUTH_NOT_READY_KEYS: Partial<Record<AuthMode, string>> = {
  basic: 'adminDash.authModeNotReadyBasic',
  ldap: 'adminDash.authModeNotReadyLdap',
  google_oauth: 'adminDash.authModeNotReadyGoogle',
}

// JSON snapshot of the auth-settings fields the Save button persists,
// plus the local-only secret-entry state (typed password / clear intent)
// so typing a new LDAP bind password or arming a clear-secret action
// also lights up the dirty indicator.
function authSnapshot(
  s: AuthSettings,
  ttlInput: string,
  ldapPwInput: string,
  ldapClear: boolean,
  googleSecretInput: string,
  googleClear: boolean,
): string {
  return JSON.stringify({
    mode: s.mode,
    ldap_server_uri: s.ldap_server_uri,
    ldap_bind_dn: s.ldap_bind_dn,
    ldap_user_base_dn: s.ldap_user_base_dn,
    ldap_user_filter: s.ldap_user_filter,
    ldap_use_starttls: s.ldap_use_starttls,
    google_client_id: s.google_client_id,
    google_allowed_domains: s.google_allowed_domains,
    google_allowed_emails: s.google_allowed_emails,
    login_message: s.login_message,
    login_button_label: s.login_button_label,
    lan_direct_noauth_mode: s.lan_direct_noauth_mode,
    session_ttl_seconds: ttlInput,
    ldap_bind_password_input: ldapPwInput,
    ldap_clear_bind_password: ldapClear,
    google_client_secret_input: googleSecretInput,
    google_clear_client_secret: googleClear,
  })
}

// The Network tab's app-access authentication settings: the login mode and
// its configuration, saved by their own Save button, plus the basic-auth
// user list and the bulk sign-out, which act at once.
export default function AdminAuthPanel({ onDirtyChange }: {
  // Whether the form has unsaved edits (AdminNetworkAuthSection guards them).
  onDirtyChange: (dirty: boolean) => void
}) {
  const { t } = useI18n()
  const [authSettings, setAuthSettings] = useState<AuthSettings | null>(null)
  // null = no load failure; otherwise the localized reason ('' when none).
  const [authLoadErr, setAuthLoadErr] = useState<string | null>(null)
  const [authUsers, setAuthUsers] = useState<AuthUser[]>([])
  const [authMsg, setAuthMsg] = useState('')
  // Non-blocking warning from the last auth-settings save: the chosen mode
  // cannot admit anyone yet (no basic users, LDAP / Google config missing).
  // Admins keep their JWT, so without this the lock-out goes unnoticed while
  // every other visitor is stuck at a login that cannot succeed.
  const [authWarning, setAuthWarning] = useState('')
  const [authMsgIsError, setAuthMsgIsError] = useState(false)
  const [authLdapBindPwInput, setAuthLdapBindPwInput] = useState('')
  const [authLdapClearBindPw, setAuthLdapClearBindPw] = useState(false)
  const [authGoogleSecretInput, setAuthGoogleSecretInput] = useState('')
  const [authGoogleClearSecret, setAuthGoogleClearSecret] = useState(false)
  const [authSessionTtlInput, setAuthSessionTtlInput] = useState('')
  const [authNewUsername, setAuthNewUsername] = useState('')
  const [authNewPassword, setAuthNewPassword] = useState('')
  const [ldapTestUsername, setLdapTestUsername] = useState('')
  const [ldapTestPassword, setLdapTestPassword] = useState('')
  const ldapTestRequest = useInFlight()
  const ldapTestBusy = ldapTestRequest.busy
  // With the username it was run for: the field can change after the test.
  const [ldapTestResult, setLdapTestResult] = useState<(LdapTestResult & { user?: string }) | null>(null)
  // Inline password-change row (not window.prompt, so the typed value is masked).
  const [pwChangeUserId, setPwChangeUserId] = useState<number | null>(null)
  const [pwChangeValue, setPwChangeValue] = useState('')
  const pwChangeRequest = useInFlight()
  const pwChangeBusy = pwChangeRequest.busy
  // In-flight guard for "Add user" so a double-click doesn't fire two POSTs;
  // the second's duplicate-username rejection would clobber the first's success
  // message with a spurious error (mirrors pwChangeBusy above).
  const addRequest = useInFlight()
  const authAddBusy = addRequest.busy
  // In-flight guard and outcome of "Sign everyone out".
  const revokeRequest = useInFlight()
  const deleteRequest = useInFlight()
  const revokeBusy = revokeRequest.busy
  const [revokeMsg, setRevokeMsg] = useState<{ text: string; isError: boolean } | null>(null)
  const saveRequest = useInFlight()
  const authSaving = saveRequest.busy
  const [authSavedSnapshot, setAuthSavedSnapshot] = useState('')
  const [authReloadKey, setAuthReloadKey] = useState(0)

  useEffect(() => {
    const ctrl = new AbortController()
    setAuthLoadErr(null)
    adminGetAuthSettings({ signal: ctrl.signal })
      .then((s) => {
        setAuthSettings(s)
        const ttl = String(s.session_ttl_seconds ?? 86400)
        setAuthSessionTtlInput(ttl)
        setAuthSavedSnapshot(authSnapshot(s, ttl, '', false, '', false))
      })
      .catch((err) => { if (!isAbortError(err)) setAuthLoadErr(apiErrorMessage(err, t)) })
    adminListAuthUsers({ signal: ctrl.signal })
      .then(setAuthUsers)
      .catch((err) => { if (!isAbortError(err)) logger.error(err) })
    return () => ctrl.abort()
  }, [authReloadKey])

  // Dirty only after the initial GET has filled in the saved snapshot, so
  // pre-load state can't claim "unsaved changes".
  const authIsDirty = !!authSavedSnapshot && !!authSettings
    && authSnapshot(authSettings, authSessionTtlInput, authLdapBindPwInput, authLdapClearBindPw, authGoogleSecretInput, authGoogleClearSecret) !== authSavedSnapshot
  useEffect(() => {
    onDirtyChange(authIsDirty)
  }, [authIsDirty, onDirtyChange])

  return (
    <div style={SECTION_STYLE}>
      <div style={SECTION_TITLE_STYLE}>{t('adminDash.auth')}</div>

      {authLoadErr !== null && !authSettings ? (
        <div role="alert" style={{ fontSize: 12, display: 'grid', gap: 6 }}>
          <div style={{ color: 'var(--color-danger-fg)' }}>{t('adminDash.authLoadFailed')}</div>
          {authLoadErr && <div>{authLoadErr}</div>}
          <div>
            <button type="button" onClick={() => setAuthReloadKey((k) => k + 1)} style={{ fontSize: 12 }}>
              {t('adminDash.retry')}
            </button>
          </div>
        </div>
      ) : !authSettings ? (
        <div style={{ fontSize: 12, opacity: 0.6 }}>{t('adminDash.loading')}</div>
      ) : (
        // Locked while a save is in flight, like the network settings above.
        <LockFieldset locked={authSaving} style={{ display: 'grid', gap: 10 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
            <span style={{ color: 'var(--color-text-muted)', flexShrink: 0 }}>{t('adminDash.authMode')}</span>
            <select
              value={authSettings.mode}
              onChange={(e) => setAuthSettings((s) => s && { ...s, mode: e.target.value as AuthMode })}
              style={{ fontSize: 12 }}
            >
              <option value="none">{t('adminDash.authModeNone')}</option>
              <option value="basic">{t('adminDash.authModeBasic')}</option>
              <option value="ldap">{t('adminDash.authModeLdap')}</option>
              <option value="google_oauth">{t('adminDash.authModeGoogle')}</option>
            </select>
          </label>

          <div style={{ fontSize: 11, color: 'var(--color-text-muted)' }}>
            {t('adminDash.authModeHelp')}
          </div>

          {authSettings.mode === 'basic' && (
            <div style={{ display: 'grid', gap: 6 }}>
              <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.authBasicUsers')}</div>
              {authUsers.length > 0 && (
                <div style={{ border: '1px solid var(--color-border)', borderRadius: 6, overflow: 'hidden' }}>
                  {authUsers.map((u, i) => (
                    <div key={u.id} style={{
                      display: 'flex', flexDirection: 'column', gap: 6,
                      padding: '6px 10px', fontSize: 12,
                      background: i % 2 === 0 ? 'var(--color-surface)' : 'var(--color-surface-muted)',
                      borderBottom: i < authUsers.length - 1 ? '1px solid var(--color-border)' : 'none',
                    }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <code style={{ fontSize: 12, flex: 1 }}>{u.username}</code>
                        <button
                          type="button"
                          // Not while a change is in flight: its success
                          // closes the open row and clears its text.
                          disabled={pwChangeBusy}
                          onClick={() => {
                            setPwChangeUserId(u.id === pwChangeUserId ? null : u.id)
                            setPwChangeValue('')
                            setAuthMsg('')
                            setAuthMsgIsError(false)
                          }}
                          style={{ fontSize: 11, padding: '0 6px' }}
                        >{t('adminDash.authChangePassword')}</button>
                        <button
                          type="button"
                          onClick={async () => {
                            if (!window.confirm(t('adminDash.authConfirmDeleteUser', { user: u.username }))) return
                            setAuthMsg('')
                            setAuthMsgIsError(false)
                            await deleteRequest.run(async () => {
                              try {
                                await adminDeleteAuthUser(u.id)
                                setAuthUsers((prev) => prev.filter((r) => r.id !== u.id))
                              } catch (e: any) {
                                setAuthMsg(apiErrorMessage(e, t) || t('adminDash.saveFailedGeneric'))
                                setAuthMsgIsError(true)
                              }
                            })
                          }}
                          // One delete at a time: a second click would only
                          // get a "not found" for the user just deleted.
                          disabled={deleteRequest.busy}
                          style={{ fontSize: 11, color: 'var(--color-danger-500)', background: 'none', border: 'none', cursor: 'pointer' }}
                        >×</button>
                      </div>
                      {pwChangeUserId === u.id && (
                        // Locked in flight: the success closes the row, and
                        // the password set is the one sent.
                        <LockFieldset locked={pwChangeBusy} style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                          <span style={{ fontSize: 11, color: 'var(--color-text-muted)' }}>
                            {t('adminDash.authChangePasswordTitle', { user: u.username })}
                          </span>
                          <input
                            type="password"
                            autoFocus
                            value={pwChangeValue}
                            onChange={(e) => setPwChangeValue(e.target.value)}
                            placeholder={t('adminDash.authNewPasswordLabel')}
                            style={{ fontSize: 12, flex: '1 1 160px' }}
                          />
                          <button
                            type="button"
                            disabled={pwChangeBusy || !pwChangeValue}
                            onClick={async () => {
                              if (!pwChangeValue) return
                              setAuthMsg('')
                              setAuthMsgIsError(false)
                              await pwChangeRequest.run(async () => {
                                try {
                                  await adminUpdateAuthUserPassword(u.id, pwChangeValue)
                                  setPwChangeUserId(null)
                                  setPwChangeValue('')
                                  setAuthMsg(t('adminDash.authUserPasswordChanged'))
                                  setAuthMsgIsError(false)
                                } catch (e: any) {
                                  setAuthMsg(apiErrorMessage(e, t) || t('adminDash.saveFailedGeneric'))
                                  setAuthMsgIsError(true)
                                }
                              })
                            }}
                            style={{ fontSize: 11 }}
                          >{t('adminDash.authConfirmChange')}</button>
                          <button
                            type="button"
                            onClick={() => { setPwChangeUserId(null); setPwChangeValue('') }}
                            style={{ fontSize: 11 }}
                          >{t('adminDash.authCancel')}</button>
                        </LockFieldset>
                      )}
                    </div>
                  ))}
                </div>
              )}
              {/* Locked in flight: the success clears both fields. */}
              <LockFieldset locked={authAddBusy} style={{ display: 'flex', gap: 6, alignItems: 'flex-end', flexWrap: 'wrap' }}>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: '1 1 120px' }}>
                  <span style={FIELD_LABEL_STYLE}>{t('adminDash.authFieldUsername')}</span>
                  <input
                    value={authNewUsername}
                    onChange={(e) => setAuthNewUsername(e.target.value)}
                    style={{ fontSize: 12, width: '100%', boxSizing: 'border-box' }}
                  />
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: '1 1 120px' }}>
                  <span style={FIELD_LABEL_STYLE}>{t('adminDash.authFieldPassword')}</span>
                  <input
                    type="password"
                    value={authNewPassword}
                    onChange={(e) => setAuthNewPassword(e.target.value)}
                    style={{ fontSize: 12, width: '100%', boxSizing: 'border-box' }}
                  />
                </label>
                <button
                  type="button"
                  disabled={authAddBusy || !authNewUsername.trim() || !authNewPassword}
                  onClick={async () => {
                    const u = authNewUsername.trim()
                    if (!u || !authNewPassword || authAddBusy) return
                    setAuthMsg('')
                    setAuthMsgIsError(false)
                    await addRequest.run(async () => {
                      try {
                        const created = await adminAddAuthUser(u, authNewPassword)
                        setAuthNewUsername('')
                        setAuthNewPassword('')
                        // Keep the list in the same username-asc order the
                        // GET endpoint serves so the inserted row lands where
                        // a re-fetch would have put it.
                        setAuthUsers((prev) =>
                          [...prev, created].sort((a, b) => a.username.localeCompare(b.username))
                        )
                        setAuthMsg(t('adminDash.authUserAdded'))
                        // A first login user settles the "basic mode has no
                        // users" warning from the last save.
                        if (authSettings?.mode === 'basic') setAuthWarning('')
                        setAuthMsgIsError(false)
                      } catch (e: any) {
                        setAuthMsg(apiErrorMessage(e, t) || t('adminDash.saveFailedGeneric'))
                        setAuthMsgIsError(true)
                      }
                    })
                  }}
                  style={{ fontSize: 12 }}
                >{t('adminDash.authAddUser')}</button>
              </LockFieldset>
            </div>
          )}

          {authSettings.mode === 'ldap' && (
            <div style={{ display: 'grid', gap: 6 }}>
              <label style={{ display: 'grid', gap: 2 }}>
                <span style={FIELD_LABEL_STYLE}>{t('adminDash.authLdapServer')}</span>
                <input
                  value={authSettings.ldap_server_uri}
                  onChange={(e) => setAuthSettings((s) => s && { ...s, ldap_server_uri: e.target.value })}
                  placeholder="ldap://ldap.example.com"
                  style={{ fontSize: 12 }}
                />
              </label>
              <label style={{ display: 'grid', gap: 2 }}>
                <span style={FIELD_LABEL_STYLE}>{t('adminDash.authLdapBindDn')}</span>
                <input
                  value={authSettings.ldap_bind_dn}
                  onChange={(e) => setAuthSettings((s) => s && { ...s, ldap_bind_dn: e.target.value })}
                  placeholder="cn=admin,dc=example,dc=com"
                  style={{ fontSize: 12 }}
                />
              </label>
              <label style={{ display: 'grid', gap: 2 }}>
                <span style={FIELD_LABEL_STYLE}>{t('adminDash.authLdapBindPassword')}</span>
                <input
                  type="password"
                  value={authLdapBindPwInput}
                  onChange={(e) => { setAuthLdapBindPwInput(e.target.value); if (e.target.value) setAuthLdapClearBindPw(false) }}
                  placeholder={authSettings.ldap_bind_password_configured ? t('adminDash.authSecretConfigured') : ''}
                  disabled={authLdapClearBindPw}
                  style={{ fontSize: 12 }}
                />
                {authSettings.ldap_bind_password_configured && !authLdapBindPwInput && (
                  authLdapClearBindPw ? (
                    <span style={{ fontSize: 11, color: 'var(--color-danger-fg)' }}>
                      {t('adminDash.authClearSecretPending')}
                      <button
                        type="button"
                        onClick={() => setAuthLdapClearBindPw(false)}
                        style={{ marginLeft: 8, fontSize: 11 }}
                      >{t('adminDash.authCancel')}</button>
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setAuthLdapClearBindPw(true)}
                      style={{ fontSize: 11, alignSelf: 'flex-start', background: 'none', border: '1px solid var(--color-border)', borderRadius: 4, padding: '2px 6px', cursor: 'pointer' }}
                    >{t('adminDash.authClearSecret')}</button>
                  )
                )}
              </label>
              <label style={{ display: 'grid', gap: 2 }}>
                <span style={FIELD_LABEL_STYLE}>{t('adminDash.authLdapBaseDn')}</span>
                <input
                  value={authSettings.ldap_user_base_dn}
                  onChange={(e) => setAuthSettings((s) => s && { ...s, ldap_user_base_dn: e.target.value })}
                  placeholder="ou=people,dc=example,dc=com"
                  style={{ fontSize: 12 }}
                />
              </label>
              <label style={{ display: 'grid', gap: 2 }}>
                <span style={FIELD_LABEL_STYLE}>{t('adminDash.authLdapFilter')}</span>
                <input
                  value={authSettings.ldap_user_filter}
                  onChange={(e) => setAuthSettings((s) => s && { ...s, ldap_user_filter: e.target.value })}
                  placeholder="(uid={username})"
                  style={{ fontSize: 12 }}
                />
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
                <input
                  type="checkbox"
                  checked={authSettings.ldap_use_starttls}
                  onChange={(e) => setAuthSettings((s) => s && { ...s, ldap_use_starttls: e.target.checked })}
                />
                {t('adminDash.authLdapStartTls')}
              </label>

              {/* LDAP connection test — uses the SAVED snapshot, so the
                  operator must press the Save Authentication button below
                  before testing has any effect. */}
              <div style={{ marginTop: 8, padding: 8, border: '1px dashed var(--color-border)', borderRadius: 6, display: 'grid', gap: 6 }}>
                <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>
                  {t('adminDash.authLdapTestHeader')}
                </div>
                <div style={{ fontSize: 11, color: 'var(--color-text-muted)' }}>
                  {t('adminDash.authLdapTestHelp')}
                </div>
                {/* Locked while the test runs: a success clears the password,
                    which would wipe one typed meanwhile. */}
                <LockFieldset locked={ldapTestBusy} style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'flex-end' }}>
                  <label style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: '1 1 140px' }}>
                    <span style={FIELD_LABEL_STYLE}>{t('adminDash.authLdapTestUsername')}</span>
                    <input
                      value={ldapTestUsername}
                      onChange={(e) => setLdapTestUsername(e.target.value)}
                      placeholder={t('adminDash.authLdapTestUsernamePlaceholder')}
                      autoComplete="off"
                      style={{ fontSize: 12, width: '100%', boxSizing: 'border-box' }}
                    />
                  </label>
                  <label style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: '1 1 140px' }}>
                    <span style={FIELD_LABEL_STYLE}>{t('adminDash.authLdapTestPassword')}</span>
                    <input
                      type="password"
                      value={ldapTestPassword}
                      onChange={(e) => setLdapTestPassword(e.target.value)}
                      autoComplete="new-password"
                      style={{ fontSize: 12, width: '100%', boxSizing: 'border-box' }}
                    />
                  </label>
                  <button
                    type="button"
                    disabled={ldapTestBusy}
                    onClick={async () => {
                      if (ldapTestBusy) return
                      // Require both or neither — matches the server
                      // contract and prevents the operator from
                      // accidentally testing only half a credential.
                      if ((ldapTestUsername && !ldapTestPassword) || (!ldapTestUsername && ldapTestPassword)) {
                        setLdapTestResult({ ok: false, stage: 'config', detail: t('adminDash.authLdapTestBothRequired') })
                        return
                      }
                      await ldapTestRequest.run(async () => {
                        setLdapTestResult(null)
                        try {
                          const res = await adminTestLdap(ldapTestUsername, ldapTestPassword)
                          setLdapTestResult({ ...res, user: ldapTestUsername })
                          // Clear the test password once it's been used so
                          // it doesn't linger in the DOM after success.
                          if (res.ok) setLdapTestPassword('')
                        } catch (e: any) {
                          setLdapTestResult({ ok: false, stage: 'config', detail: apiErrorMessage(e, t) || t('adminDash.saveFailedGeneric') })
                        }
                      })
                    }}
                    style={{ fontSize: 12 }}
                  >
                    {ldapTestBusy ? t('adminDash.authLdapTesting') : t('adminDash.authLdapTestButton')}
                  </button>
                </LockFieldset>
                {ldapTestResult && (
                  <div
                    role="status"
                    style={{
                      fontSize: 11,
                      padding: '6px 8px',
                      borderRadius: 4,
                      whiteSpace: 'pre-wrap',
                      background: ldapTestResult.ok ? 'var(--color-success-bg)' : 'var(--color-danger-bg)',
                      color: ldapTestResult.ok ? 'var(--color-success-fg)' : 'var(--color-danger-fg)',
                    }}
                  >
                    {ldapTestResult.ok
                      ? (ldapTestResult.user
                          ? t('adminDash.authLdapTestOkUser', { user: ldapTestResult.user })
                          : t('adminDash.authLdapTestOk'))
                      : t('adminDash.authLdapTestFailed', {
                          stage: t(`adminDash.authLdapTestStage.${ldapTestResult.stage}`),
                          detail: ldapTestResult.detail || '-',
                        })}
                  </div>
                )}
              </div>
            </div>
          )}

          {authSettings.mode === 'google_oauth' && (
            <div style={{ display: 'grid', gap: 6 }}>
              <label style={{ display: 'grid', gap: 2 }}>
                <span style={FIELD_LABEL_STYLE}>{t('adminDash.authGoogleClientId')}</span>
                <input
                  value={authSettings.google_client_id}
                  onChange={(e) => setAuthSettings((s) => s && { ...s, google_client_id: e.target.value })}
                  style={{ fontSize: 12 }}
                />
              </label>
              <label style={{ display: 'grid', gap: 2 }}>
                <span style={FIELD_LABEL_STYLE}>{t('adminDash.authGoogleClientSecret')}</span>
                <input
                  type="password"
                  value={authGoogleSecretInput}
                  onChange={(e) => { setAuthGoogleSecretInput(e.target.value); if (e.target.value) setAuthGoogleClearSecret(false) }}
                  placeholder={authSettings.google_client_secret_configured ? t('adminDash.authSecretConfigured') : ''}
                  disabled={authGoogleClearSecret}
                  style={{ fontSize: 12 }}
                />
                {authSettings.google_client_secret_configured && !authGoogleSecretInput && (
                  authGoogleClearSecret ? (
                    <span style={{ fontSize: 11, color: 'var(--color-danger-fg)' }}>
                      {t('adminDash.authClearSecretPending')}
                      <button
                        type="button"
                        onClick={() => setAuthGoogleClearSecret(false)}
                        style={{ marginLeft: 8, fontSize: 11 }}
                      >{t('adminDash.authCancel')}</button>
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => setAuthGoogleClearSecret(true)}
                      style={{ fontSize: 11, alignSelf: 'flex-start', background: 'none', border: '1px solid var(--color-border)', borderRadius: 4, padding: '2px 6px', cursor: 'pointer' }}
                    >{t('adminDash.authClearSecret')}</button>
                  )
                )}
              </label>
              <label style={{ display: 'grid', gap: 2 }}>
                <span style={FIELD_LABEL_STYLE}>{t('adminDash.authGoogleAllowedDomains')}</span>
                <input
                  value={authSettings.google_allowed_domains}
                  onChange={(e) => setAuthSettings((s) => s && { ...s, google_allowed_domains: e.target.value })}
                  placeholder="example.com,example.org"
                  style={{ fontSize: 12 }}
                />
              </label>
              <label style={{ display: 'grid', gap: 2 }}>
                <span style={FIELD_LABEL_STYLE}>{t('adminDash.authGoogleAllowedEmails')}</span>
                <input
                  value={authSettings.google_allowed_emails}
                  onChange={(e) => setAuthSettings((s) => s && { ...s, google_allowed_emails: e.target.value })}
                  placeholder="alice@example.com,bob@example.net"
                  style={{ fontSize: 12 }}
                />
              </label>
              <div style={{ fontSize: 11, color: 'var(--color-text-muted)', background: 'var(--color-surface-muted)', borderRadius: 5, padding: '6px 10px' }}>
                {t('adminDash.authGoogleCallbackHint', { url: `${window.location.origin}/api/auth/oauth/callback` })}
              </div>
            </div>
          )}

          {/* Login screen customization (applies to every mode that shows a
              login screen; empty falls back to the built-in defaults). */}
          {authSettings.mode !== 'none' && (
            <div style={{ display: 'grid', gap: 6, marginTop: 4, paddingTop: 8, borderTop: '1px solid var(--color-border)' }}>
              <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>
                {t('adminDash.authLoginScreen')}
              </div>
              <label style={{ display: 'grid', gap: 2 }}>
                <span style={FIELD_LABEL_STYLE}>{t('adminDash.authLoginMessage')}</span>
                <textarea
                  value={authSettings.login_message}
                  onChange={(e) => setAuthSettings((s) => s && { ...s, login_message: e.target.value })}
                  placeholder={t('adminDash.authLoginMessagePlaceholder')}
                  rows={2}
                  style={{ fontSize: 12, resize: 'vertical', fontFamily: 'inherit' }}
                />
                <span style={{ fontSize: 11, color: 'var(--color-text-muted)' }}>{t('adminDash.authLoginMessageHelp')}</span>
              </label>
              <label style={{ display: 'grid', gap: 2 }}>
                <span style={FIELD_LABEL_STYLE}>{t('adminDash.authLoginButtonLabel')}</span>
                <input
                  value={authSettings.login_button_label}
                  onChange={(e) => setAuthSettings((s) => s && { ...s, login_button_label: e.target.value })}
                  placeholder={t('adminDash.authLoginButtonLabelPlaceholder')}
                  maxLength={128}
                  style={{ fontSize: 12 }}
                />
                <span style={{ fontSize: 11, color: 'var(--color-text-muted)' }}>{t('adminDash.authLoginButtonLabelHelp')}</span>
              </label>
            </div>
          )}

          {/* Direct LAN access exemption: choose how far it reaches
              (off / bare-IP only / any LAN-direct incl. hostname). */}
          <div style={{ display: 'grid', gap: 4, marginTop: 4, paddingTop: 8, borderTop: '1px solid var(--color-border)' }}>
            <label style={{ display: 'grid', gap: 2 }}>
              <span style={{ fontSize: 12, fontWeight: 600 }}>{t('adminDash.authLanDirectNoauth')}</span>
              <select
                value={authSettings.lan_direct_noauth_mode}
                onChange={(e) => setAuthSettings((s) => s && { ...s, lan_direct_noauth_mode: e.target.value as AuthSettings['lan_direct_noauth_mode'] })}
                style={{ fontSize: 12, width: 'fit-content' }}
              >
                <option value="off">{t('adminDash.authLanDirectNoauthOptOff')}</option>
                <option value="ip">{t('adminDash.authLanDirectNoauthOptIp')}</option>
                <option value="lan">{t('adminDash.authLanDirectNoauthOptLan')}</option>
              </select>
            </label>
            <span style={{ fontSize: 11, color: 'var(--color-text-muted)' }}>{t('adminDash.authLanDirectNoauthHelp')}</span>
          </div>

          <label style={{ display: 'grid', gap: 2 }}>
            <span style={FIELD_LABEL_STYLE}>{t('adminDash.authSessionTtl')}</span>
            <input
              type="number"
              min={60}
              max={2592000}
              value={authSessionTtlInput}
              onChange={(e) => setAuthSessionTtlInput(e.target.value)}
              style={{ fontSize: 12, width: 160 }}
            />
            <span style={{ fontSize: 11, color: 'var(--color-text-muted)' }}>{t('adminDash.authSessionTtlHelp')}</span>
          </label>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginTop: 4 }}>
          <button
            type="button"
            disabled={authSaving || !authIsDirty}
            style={authIsDirty ? DIRTY_SAVE_STYLE : undefined}
            onClick={async () => {
              if (authSaving) return
              setAuthMsg('')
              setAuthWarning('')
              await saveRequest.run(async () => {
                try {
                  const payload: Parameters<typeof adminUpdateAuthSettings>[0] = {
                    mode: authSettings.mode,
                    ldap_server_uri: authSettings.ldap_server_uri,
                    ldap_bind_dn: authSettings.ldap_bind_dn,
                    ldap_user_base_dn: authSettings.ldap_user_base_dn,
                    ldap_user_filter: authSettings.ldap_user_filter,
                    ldap_use_starttls: authSettings.ldap_use_starttls,
                    google_client_id: authSettings.google_client_id,
                    google_allowed_domains: authSettings.google_allowed_domains,
                    google_allowed_emails: authSettings.google_allowed_emails,
                    login_message: authSettings.login_message,
                    login_button_label: authSettings.login_button_label,
                    lan_direct_noauth_mode: authSettings.lan_direct_noauth_mode,
                  }
                  // Only include the secrets when the operator typed
                  // a new value, so blank fields don't wipe stored
                  // secrets on every save. Use clear_* to actively wipe.
                  if (authLdapBindPwInput) payload.ldap_bind_password = authLdapBindPwInput
                  else if (authLdapClearBindPw) payload.clear_ldap_bind_password = true
                  if (authGoogleSecretInput) payload.google_client_secret = authGoogleSecretInput
                  else if (authGoogleClearSecret) payload.clear_google_client_secret = true
                  const ttlNum = parseInt(authSessionTtlInput, 10)
                  if (Number.isFinite(ttlNum)) payload.session_ttl_seconds = ttlNum
                  const { warning, ...saved } = await adminUpdateAuthSettings(payload)
                  setAuthSettings(saved)
                  // The server's text is English-only; show the localized
                  // message for the mode it refers to, raw text as fallback.
                  if (warning) setAuthWarning(AUTH_NOT_READY_KEYS[saved.mode] ? t(AUTH_NOT_READY_KEYS[saved.mode]!) : warning)
                  const ttl = String(saved.session_ttl_seconds ?? 86400)
                  setAuthSessionTtlInput(ttl)
                  setAuthLdapBindPwInput('')
                  setAuthLdapClearBindPw(false)
                  setAuthGoogleSecretInput('')
                  setAuthGoogleClearSecret(false)
                  setAuthSavedSnapshot(authSnapshot(saved, ttl, '', false, '', false))
                  setAuthMsg(t('adminDash.authSaved'))
                  setAuthMsgIsError(false)
                } catch (e: any) {
                  setAuthMsg(apiErrorMessage(e, t) || t('adminDash.saveFailedGeneric'))
                  setAuthMsgIsError(true)
                }
              })
            }}
          >
            {authSaving ? t('adminDash.saving') : t('adminDash.authSave')}
          </button>
          {authIsDirty && !authSaving && <UnsavedChangesChip />}
          </div>
          <SaveResult text={authMsg} isError={authMsgIsError} />
          {authWarning && (
            <div role="alert" style={{ fontSize: 12, padding: '6px 8px', borderRadius: 4, whiteSpace: 'pre-wrap', background: 'var(--color-warning-bg, #fef3c7)', color: 'var(--color-warning-fg, #92400e)' }}>
              {authWarning}
            </div>
          )}

          {/* Bulk sign-out. The request carries only the revoke flag: the
              server bumps the app-session generation, so every visitor
              login cookie issued so far stops working, and changes no
              setting. The form's unsaved edits are left alone. */}
          {authSettings.mode !== 'none' && (
            <div style={{ display: 'grid', gap: 4, marginTop: 4, paddingTop: 8, borderTop: '1px solid var(--color-border)' }}>
              <span style={{ fontSize: 12, fontWeight: 600 }}>{t('adminDash.authRevokeSessions')}</span>
              <span style={{ fontSize: 11, color: 'var(--color-text-muted)' }}>{t('adminDash.authRevokeSessionsHelp')}</span>
              <div>
                <button
                  type="button"
                  disabled={revokeBusy}
                  onClick={async () => {
                    if (revokeBusy || !window.confirm(t('adminDash.authRevokeSessionsConfirm'))) return
                    setRevokeMsg(null)
                    await revokeRequest.run(async () => {
                      try {
                        await adminUpdateAuthSettings({ revoke_app_sessions: true })
                        setRevokeMsg({ text: t('adminDash.authRevokeSessionsDone'), isError: false })
                      } catch (e) {
                        setRevokeMsg({ text: failureMessage(t('adminDash.authRevokeSessionsFailed'), e, t), isError: true })
                      }
                    })
                  }}
                  style={{ fontSize: 12, color: 'var(--color-danger-fg)' }}
                >
                  {revokeBusy ? t('adminDash.authRevokingSessions') : t('adminDash.authRevokeSessions')}
                </button>
              </div>
              {revokeMsg && (
                <div role="status" style={{ fontSize: 12, padding: '6px 8px', borderRadius: 4, whiteSpace: 'pre-wrap', background: revokeMsg.isError ? 'var(--color-danger-bg)' : 'var(--color-success-bg)' }}>
                  {revokeMsg.text}
                </div>
              )}
            </div>
          )}
        </LockFieldset>
      )}
    </div>
  )
}
