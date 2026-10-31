import React, { useEffect, useMemo, useState } from 'react'
import {
  AdminMe,
  ManagedUser,
  Role,
  RolePermissions,
  adminCreateUser,
  adminDeleteUser,
  adminGetRolePermissions,
  adminListUsers,
  adminResetUserMfa,
  adminSetUserActive,
  adminUpdateRolePermissions,
  isAbortError,
} from '../../lib/api'
import { Badge, Button, FormField, Input, LockFieldset, Notice, Select } from '../ui'
import { useInFlight, useInFlightKey } from '../../hooks/useInFlight'
import { logger } from '../../lib/logger'
import { formatInEmbeddedOffset } from '../../lib/admin/datetime'
import { useI18n } from '../../lib/i18n'
import { apiErrorMessage, localizedErrorDetail, withErrorDetail } from '../../lib/admin/apiError'

const ROLE_TONE: Record<Role, 'brand' | 'success' | 'info'> = {
  admin: 'brand',
  manager: 'success',
  instructor: 'info',
}

function RoleBadge({ role }: { role: Role }) {
  const { t } = useI18n()
  return <Badge tone={ROLE_TONE[role]}>{t(`roles.${role}`)}</Badge>
}

type Props = {
  me: AdminMe
}

export default function AdminUsersSection({ me }: Props) {
  const { t } = useI18n()
  const [users, setUsers] = useState<ManagedUser[] | null>(null)
  const [perms, setPerms] = useState<RolePermissions | null>(null)
  // The error of a failed role-permissions load, so the table's place says
  // why it is empty rather than "Loading…" for ever. Kept as the error and
  // worded at render, in the language shown then.
  const [permsLoadError, setPermsLoadError] = useState<unknown>(null)
  const [loadError, setLoadError] = useState('')

  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newRole, setNewRole] = useState<Role>('instructor')
  const createRequest = useInFlight()
  const permRequest = useInFlight()
  const createBusy = createRequest.busy
  const [createMsg, setCreateMsg] = useState<{ text: string; tone: 'success' | 'error' } | null>(null)

  // One row action (enable / disable, delete) at a time; every row's buttons
  // wait for it, so a second click can't send a second request.
  const rowRequest = useInFlightKey<{ id: number; kind: 'toggle' | 'delete' }>()
  const togglingId = rowRequest.key?.kind === 'toggle' ? rowRequest.key.id : null
  const deletingId = rowRequest.key?.kind === 'delete' ? rowRequest.key.id : null

  // Two-factor reset of another member: the row whose reset is being
  // confirmed (with the acting admin's own password), and the outcome.
  const [mfaResetTarget, setMfaResetTarget] = useState<ManagedUser | null>(null)
  const [mfaResetPw, setMfaResetPw] = useState('')
  const [mfaResetBusy, setMfaResetBusy] = useState(false)
  const [mfaResetMsg, setMfaResetMsg] = useState<{ text: string; tone: 'success' | 'error' } | null>(null)

  useEffect(() => {
    const ctrl = new AbortController()
    adminListUsers({ signal: ctrl.signal })
      .then(setUsers)
      .catch((err) => {
        if (isAbortError(err)) return
        logger.error('list users failed:', err)
        setLoadError(apiErrorMessage(err, t) || t('adminUsers.loadFailed'))
      })
    adminGetRolePermissions({ signal: ctrl.signal })
      .then(setPerms)
      .catch((err) => {
        if (isAbortError(err)) return
        logger.error('get role permissions failed:', err)
        setPermsLoadError(err)
      })
    return () => ctrl.abort()
  }, [])

  // The signed-in admin's own row follows a two-factor change made in the
  // Account panel above (me.mfa), without refetching the list or disturbing
  // a form in progress here.
  const selfMfaEnabled = me.mfa?.enabled
  useEffect(() => {
    if (selfMfaEnabled === undefined) return
    setUsers((prev) => (prev
      ? prev.map((u) => (u.id === me.id && u.mfa_enabled !== selfMfaEnabled ? { ...u, mfa_enabled: selfMfaEnabled } : u))
      : prev))
  }, [selfMfaEnabled, me.id])

  const activeAdminCount = useMemo(() => {
    if (!users) return 0
    return users.filter((u) => u.role === 'admin' && u.is_active).length
  }, [users])

  async function onCreate(e: React.FormEvent) {
    e.preventDefault()
    if (createBusy) return
    const username = newUsername.trim()
    if (!username) {
      setCreateMsg({ text: t('adminUsers.usernameRequired'), tone: 'error' })
      return
    }
    if (!newPassword) {
      setCreateMsg({ text: t('adminUsers.passwordRequired'), tone: 'error' })
      return
    }
    await createRequest.run(async () => {
      setCreateMsg(null)
      try {
        const created = await adminCreateUser(username, newPassword, newRole)
        setUsers((prev) => (prev ? [...prev, created] : [created]))
        setNewUsername('')
        setNewPassword('')
        setNewRole('instructor')
        setCreateMsg({
          text: t('adminUsers.createSuccess', { username: created.username, role: t(`roles.${created.role}`) }),
          tone: 'success',
        })
      } catch (err) {
        setCreateMsg({ text: apiErrorMessage(err, t) || t('adminUsers.createFailed'), tone: 'error' })
      }
    })
  }

  async function onToggleActive(row: ManagedUser) {
    const next = !row.is_active
    const confirmKey = next ? 'adminUsers.enableConfirm' : 'adminUsers.disableConfirm'
    if (!confirm(t(confirmKey, { username: row.username }))) return
    await rowRequest.run({ id: row.id, kind: 'toggle' }, async () => {
      try {
        const updated = await adminSetUserActive(row.id, next)
        setUsers((prev) => (prev ? prev.map((u) => (u.id === updated.id ? updated : u)) : prev))
      } catch (err) {
        alert(t('adminUsers.updateFailed', { error: apiErrorMessage(err, t) }))
      }
    })
  }

  async function onDelete(row: ManagedUser) {
    if (!confirm(t('adminUsers.deleteConfirm', { username: row.username }))) return
    await rowRequest.run({ id: row.id, kind: 'delete' }, async () => {
      try {
        await adminDeleteUser(row.id)
        setUsers((prev) => (prev ? prev.filter((u) => u.id !== row.id) : prev))
      } catch (err) {
        alert(t('adminUsers.deleteFailed', { error: apiErrorMessage(err, t) }))
      }
    })
  }

  function onStartMfaReset(row: ManagedUser) {
    setMfaResetTarget(row)
    setMfaResetPw('')
    setMfaResetMsg(null)
  }

  async function onConfirmMfaReset(e: React.FormEvent) {
    e.preventDefault()
    if (!mfaResetTarget || mfaResetBusy) return
    if (!mfaResetPw) {
      setMfaResetMsg({ text: t('admin.passwordRequired'), tone: 'error' })
      return
    }
    setMfaResetBusy(true)
    try {
      const updated = await adminResetUserMfa(mfaResetTarget.id, mfaResetPw)
      setUsers((prev) => (prev ? prev.map((u) => (u.id === updated.id ? updated : u)) : prev))
      setMfaResetTarget(null)
      setMfaResetPw('')
      setMfaResetMsg({ text: t('adminUsers.resetMfaDone', { username: updated.username }), tone: 'success' })
    } catch (err) {
      setMfaResetMsg({ text: t('adminUsers.resetMfaFailed', { error: apiErrorMessage(err, t) }), tone: 'error' })
    } finally {
      setMfaResetBusy(false)
    }
  }

  function onTogglePerm(key: keyof RolePermissions, value: boolean) {
    if (!perms) return
    const prevValue = perms[key]
    return permRequest.run(async () => {
      setPerms(p => (p ? { ...p, [key]: value } : p))
      try {
        const updated = await adminUpdateRolePermissions({ [key]: value })
        // Merge only the key that changed: the response is the server's whole
        // row, and the other keys are not this request's to set.
        setPerms(p => (p ? { ...p, [key]: updated[key] } : updated))
      } catch (err) {
        logger.error('update role permissions failed:', err)
        // Roll back only this key, for the same reason.
        setPerms(p => (p ? { ...p, [key]: prevValue } : p))
        alert(t('adminUsers.updateFailed', { error: apiErrorMessage(err, t) }))
      }
    })
  }

  return (
    <>
      <h3 style={{ marginTop: 16 }}>{t('adminUsers.sectionTitle')}</h3>
      <div style={{ display: 'grid', gap: 16 }}>
        <div style={{ borderLeft: '3px solid var(--color-border-strong)', paddingLeft: 8 }}>
          <div style={{ fontSize: 13, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>
            {t('adminUsers.userListTitle')}
          </div>
          {loadError && (
            <div style={{ fontSize: 12, padding: '6px 8px', borderRadius: 4, background: 'var(--color-danger-bg)', color: 'var(--color-danger-fg)', marginBottom: 8 }}>
              {loadError}
            </div>
          )}
          {users === null && !loadError && (
            <div style={{ fontSize: 12, opacity: 0.7 }}>{t('adminUsers.loading')}</div>
          )}
          {users && users.length === 0 && (
            <div style={{ fontSize: 12, opacity: 0.7 }}>{t('adminUsers.noUsers')}</div>
          )}
          {users && users.length > 0 && (
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
              <thead>
                <tr>
                  <th style={TH}>{t('adminUsers.colUsername')}</th>
                  <th style={{ ...TH, width: 110 }}>{t('adminUsers.colRole')}</th>
                  <th style={{ ...TH, width: 130 }}>{t('adminUsers.colCreated')}</th>
                  <th style={{ ...TH, width: 250 }}></th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const isSelf = u.id === me.id
                  const isLastAdmin = u.role === 'admin' && u.is_active && activeAdminCount <= 1
                  const disabledReason = isSelf
                    ? t('adminUsers.cannotDeleteSelf')
                    : isLastAdmin
                      ? t('adminUsers.cannotDeleteLastAdmin')
                      : ''
                  const created = formatInEmbeddedOffset(u.created_at, { dateOnly: true })
                  return (
                    <tr key={u.id}>
                      <td style={TD}>
                        <span style={{ fontWeight: 500 }}>{u.username}</span>
                        {!u.is_active && <span style={{ marginLeft: 6, fontSize: 11, opacity: 0.6 }}>{t('adminUsers.inactiveBadge')}</span>}
                        {u.mfa_enabled && (
                          <span style={{ marginLeft: 6 }} title={t('adminUsers.mfaBadgeTitle')}>
                            <Badge tone="success">{t('adminUsers.mfaBadge')}</Badge>
                          </span>
                        )}
                      </td>
                      <td style={TD}><RoleBadge role={u.role} /></td>
                      <td style={{ ...TD, fontSize: 12, opacity: 0.8 }}>{created === '—' ? '' : created}</td>
                      <td style={TD}>
                        <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                          {u.mfa_enabled && (
                            <Button
                              size="sm"
                              variant="secondary"
                              disabled={isSelf || mfaResetBusy}
                              title={isSelf ? t('adminUsers.cannotResetOwnMfa') : undefined}
                              onClick={() => onStartMfaReset(u)}
                            >
                              {t('adminUsers.resetMfaButton')}
                            </Button>
                          )}
                          <Button
                            size="sm"
                            variant="secondary"
                            disabled={!!disabledReason || rowRequest.key !== null}
                            title={disabledReason || undefined}
                            onClick={() => onToggleActive(u)}
                          >
                            {togglingId === u.id
                              ? '…'
                              : u.is_active
                                ? t('adminUsers.disableButton')
                                : t('adminUsers.enableButton')}
                          </Button>
                          <Button
                            size="sm"
                            variant="danger"
                            disabled={!!disabledReason || rowRequest.key !== null}
                            title={disabledReason || undefined}
                            onClick={() => onDelete(u)}
                          >
                            {deletingId === u.id ? '…' : t('adminUsers.deleteButton')}
                          </Button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
          {mfaResetTarget && (
            <form
              onSubmit={onConfirmMfaReset}
              style={{ marginTop: 12, padding: 12, border: '1px solid var(--color-border-strong)', borderRadius: 4, display: 'grid', gap: 8, maxWidth: 480 }}
            >
              <div style={{ fontWeight: 600, fontSize: 13 }}>{t('adminUsers.resetMfaTitle', { username: mfaResetTarget.username })}</div>
              <div style={{ fontSize: 12, opacity: 0.8 }}>{t('adminUsers.resetMfaHelp')}</div>
              {/* Locked while the reset is out: its success closes the form
                  and clears the password. */}
              <LockFieldset locked={mfaResetBusy} style={{ display: 'grid', gap: 8 }}>
              <FormField label={t('adminUsers.resetMfaPassword')}>
                <Input
                  type="password"
                  value={mfaResetPw}
                  onChange={(e) => setMfaResetPw(e.target.value)}
                  autoComplete="current-password"
                  autoFocus
                />
              </FormField>
              <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end' }}>
                <Button size="sm" type="button" variant="ghost" onClick={() => { setMfaResetTarget(null); setMfaResetMsg(null) }}>
                  {t('adminUsers.resetMfaCancel')}
                </Button>
                <Button size="sm" type="submit" variant="danger" loading={mfaResetBusy}>
                  {t('adminUsers.resetMfaConfirm')}
                </Button>
              </div>
              </LockFieldset>
            </form>
          )}
          {mfaResetMsg && (
            <div style={{ marginTop: 8 }}>
              <Notice error={mfaResetMsg.tone === 'error'}>{mfaResetMsg.text}</Notice>
            </div>
          )}
        </div>

        <div style={{ borderLeft: '3px solid var(--color-border-strong)', paddingLeft: 8 }}>
          <div style={{ fontSize: 13, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>
            {t('adminUsers.createTitle')}
          </div>
          <form onSubmit={onCreate} style={{ display: 'grid', gap: 8, maxWidth: 360 }}>
            {/* Locked while the create is in flight: its success clears the
                fields, which would wipe what was typed meanwhile. */}
            <LockFieldset locked={createBusy} style={{ display: 'grid', gap: 8 }}>
            <FormField label={t('adminUsers.usernameLabel')}>
              <Input
                type="text"
                value={newUsername}
                onChange={(e) => setNewUsername(e.target.value)}
                autoComplete="off"
              />
            </FormField>
            <FormField label={t('adminUsers.passwordLabel')}>
              <Input
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                autoComplete="new-password"
              />
            </FormField>
            <FormField label={t('adminUsers.roleLabel')}>
              <Select value={newRole} onChange={(e) => setNewRole(e.target.value as Role)}>
                <option value="instructor">{t('roles.instructor')}</option>
                <option value="manager">{t('roles.manager')}</option>
                <option value="admin">{t('roles.admin')}</option>
              </Select>
            </FormField>
            </LockFieldset>
            <div>
              <Button type="submit" variant="primary" disabled={createBusy}>
                {createBusy ? t('adminUsers.creatingButton') : t('adminUsers.createTitle')}
              </Button>
            </div>
            {createMsg && (
              <div style={{
                fontSize: 12,
                padding: '6px 8px',
                borderRadius: 4,
                background: createMsg.tone === 'error' ? 'var(--color-danger-bg)' : 'var(--color-success-bg)',
                color: createMsg.tone === 'error' ? 'var(--color-danger-fg)' : 'var(--color-success-fg)',
              }}>
                {createMsg.text}
              </div>
            )}
          </form>
        </div>

        <div style={{ borderLeft: '3px solid var(--color-border-strong)', paddingLeft: 8 }}>
          <div style={{ fontSize: 13, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>
            {t('adminUsers.rolePermsTitle')}
          </div>
          <div style={{ fontSize: 12, opacity: 0.8, marginBottom: 8 }}>
            {t('adminUsers.rolePermsHelp')}
          </div>
          {perms === null && permsLoadError !== null ? (
            <div role="alert" style={{ fontSize: 12, padding: '6px 8px', borderRadius: 4, background: 'var(--color-danger-bg)', color: 'var(--color-danger-fg)' }}>
              <div>{withErrorDetail(t('adminUsers.rolePermsLoadFailed'), localizedErrorDetail(permsLoadError, t))}</div>
              <div>{t('common.reloadPageToRetry')}</div>
            </div>
          ) : perms === null ? (
            <div style={{ fontSize: 12, opacity: 0.7 }}>{t('adminUsers.loading')}</div>
          ) : (
            // One change at a time: the table is locked until it lands.
            <LockFieldset locked={permRequest.busy}>
              <table style={{ borderCollapse: 'collapse', fontSize: 13 }}>
                <thead>
                  <tr>
                    <th style={TH}></th>
                    <th style={{ ...TH, textAlign: 'center', padding: '4px 12px' }}>{t('adminUsers.colAnalytics')}</th>
                    <th style={{ ...TH, textAlign: 'center', padding: '4px 12px' }}>{t('adminUsers.colServer')}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td style={{ ...TD, paddingRight: 12 }}><RoleBadge role="manager" /></td>
                    <td style={{ ...TD, textAlign: 'center' }}>
                      <input
                        type="checkbox"
                        checked={perms.manager_can_view_analytics}
                        onChange={(e) => onTogglePerm('manager_can_view_analytics', e.target.checked)}
                      />
                    </td>
                    <td style={{ ...TD, textAlign: 'center' }}>
                      <input
                        type="checkbox"
                        checked={perms.manager_can_view_server}
                        onChange={(e) => onTogglePerm('manager_can_view_server', e.target.checked)}
                      />
                    </td>
                  </tr>
                  <tr>
                    <td style={{ ...TD, paddingRight: 12 }}><RoleBadge role="instructor" /></td>
                    <td style={{ ...TD, textAlign: 'center' }}>
                      <input
                        type="checkbox"
                        checked={perms.instructor_can_view_analytics}
                        onChange={(e) => onTogglePerm('instructor_can_view_analytics', e.target.checked)}
                      />
                    </td>
                    <td style={{ ...TD, textAlign: 'center' }}>
                      <input
                        type="checkbox"
                        checked={perms.instructor_can_view_server}
                        onChange={(e) => onTogglePerm('instructor_can_view_server', e.target.checked)}
                      />
                    </td>
                  </tr>
                </tbody>
              </table>
            </LockFieldset>
          )}
        </div>
      </div>
    </>
  )
}

const TH: React.CSSProperties = {
  textAlign: 'left',
  padding: '4px 6px',
  borderBottom: '1px solid var(--color-border)',
  fontSize: 10,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  color: 'var(--color-text-faint)',
}

const TD: React.CSSProperties = {
  padding: '6px',
  borderBottom: '1px solid var(--color-border)',
  verticalAlign: 'middle',
}
