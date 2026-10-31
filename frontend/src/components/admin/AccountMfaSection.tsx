import React, { useEffect, useState } from 'react'
import QRCode from 'qrcode'
import {
  MfaSetup,
  MfaStatus,
  adminMfaConfirm,
  adminMfaDisable,
  adminMfaRegenerateRecoveryCodes,
  adminMfaSetup,
  isLastRecoveryCodeSession,
} from '../../lib/api'
import { Badge, Button, FormField, Input, LockFieldset, Notice } from '../ui'
import { useI18n } from '../../lib/i18n'
import { apiErrorMessage } from '../../lib/admin/apiError'
import { formatInEmbeddedOffset } from '../../lib/admin/datetime'
import { copyTextToClipboard, triggerDownload } from '../../lib/clipboard'
import { logger } from '../../lib/logger'
import { useInFlight } from '../../hooks/useInFlight'

type Props = {
  username: string
  mfa: MfaStatus | undefined
  onChange: (mfa: MfaStatus) => void
}

type Msg = { text: string; error: boolean } | null

// The account owner's two-factor sign-in: turn it on (password → QR code →
// first code → recovery codes), and, while on, turn it off or replace the
// recovery codes (password + a current code). Every role has this; it is off
// until the owner turns it on.
export default function AccountMfaSection({ username, mfa, onChange }: Props) {
  const { t } = useI18n()
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [setup, setSetup] = useState<MfaSetup | null>(null)
  const [qr, setQr] = useState('')
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null)
  const [copied, setCopied] = useState(false)
  // One request at a time; the form that sent it is locked until it lands
  // (its success or failure clears the code and password typed into it).
  const request = useInFlight()
  const busy = request.busy
  const [msg, setMsg] = useState<Msg>(null)
  // Signed in with the last recovery code: no code is left to give, and this
  // session may go on with the password alone (the server decides).
  const noCodesLeft = !!mfa?.enabled && (mfa?.recovery_codes_remaining ?? 0) === 0
    && isLastRecoveryCodeSession()

  useEffect(() => {
    if (!setup) { setQr(''); return }
    let live = true
    QRCode.toDataURL(setup.otpauth_uri, { width: 192, margin: 2 })
      .then((url) => { if (live) setQr(url) })
      .catch((err) => logger.error('QR render failed:', err))
    return () => { live = false }
  }, [setup])

  function fail(err: unknown) {
    setMsg({ text: apiErrorMessage(err, t) || t('admin.updateFailed'), error: true })
  }

  async function onStart(e: React.FormEvent) {
    e.preventDefault()
    if (busy) return
    if (!password) { setMsg({ text: t('admin.passwordRequired'), error: true }); return }
    await request.run(async () => {
      setMsg(null)
      try {
        setSetup(await adminMfaSetup(password))
        setPassword('')
        setCode('')
      } catch (err) {
        fail(err)
      }
    })
  }

  async function onConfirm(e: React.FormEvent) {
    e.preventDefault()
    if (busy || !setup) return
    if (!code.trim()) { setMsg({ text: t('adminMfa.codeRequired'), error: true }); return }
    await request.run(async () => {
      setMsg(null)
      try {
        const r = await adminMfaConfirm(setup.setup_token, code)
        setSetup(null)
        setCode('')
        setRecoveryCodes(r.recovery_codes)
        onChange(r.mfa)
        setMsg({ text: t('adminMfa.enabledDone'), error: false })
      } catch (err) {
        setCode('')
        fail(err)
      }
    })
  }

  function onCancelSetup() {
    setSetup(null)
    setCode('')
    setMsg(null)
  }

  async function onManage(action: 'disable' | 'regenerate') {
    if (busy) return
    if (!password) { setMsg({ text: t('admin.passwordRequired'), error: true }); return }
    // With no recovery code left, a session that signed in with the last one
    // may go on without a code (the server decides; see admin_mfa).
    if (!code.trim() && !noCodesLeft) { setMsg({ text: t('adminMfa.codeRequired'), error: true }); return }
    if (!window.confirm(t(action === 'disable' ? 'adminMfa.disableConfirm' : 'adminMfa.regenerateConfirm'))) return
    await request.run(async () => {
      setMsg(null)
      try {
        if (action === 'disable') {
          const r = await adminMfaDisable(password, code)
          onChange(r.mfa)
          setRecoveryCodes(null)
          setMsg({ text: t('adminMfa.disabledDone'), error: false })
        } else {
          const r = await adminMfaRegenerateRecoveryCodes(password, code)
          onChange(r.mfa)
          setRecoveryCodes(r.recovery_codes)
          setMsg(null)
        }
        setPassword('')
        setCode('')
      } catch (err) {
        setCode('')
        fail(err)
      }
    })
  }

  async function onCopyCodes() {
    if (!recoveryCodes) return
    try {
      await copyTextToClipboard(recoveryCodes.join('\n'))
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch (err) {
      logger.error('copy failed:', err)
    }
  }

  function onDownloadCodes() {
    if (!recoveryCodes) return
    const body = `${t('adminMfa.recoveryTitle')} — ${username}\n\n${recoveryCodes.join('\n')}\n`
    const url = URL.createObjectURL(new Blob([body], { type: 'text/plain' }))
    triggerDownload(url, `recovery-codes-${username}.txt`)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  const enabled = !!mfa?.enabled
  const enabledAt = mfa?.enabled_at ? formatInEmbeddedOffset(mfa.enabled_at, { dateOnly: true }) : ''

  return (
    <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 'var(--space-2)', fontSize: 'var(--font-size-sm)' }}>
        <Badge tone={enabled ? 'success' : 'neutral'}>
          {enabled ? t('adminMfa.statusOn') : t('adminMfa.statusOff')}
        </Badge>
        {enabled && enabledAt && enabledAt !== '—' && (
          <span style={{ color: 'var(--color-text-muted)' }}>{t('adminMfa.enabledSince', { date: enabledAt })}</span>
        )}
        {enabled && (
          <span style={{ color: 'var(--color-text-muted)' }}>
            {t('adminMfa.recoveryRemaining', { n: mfa?.recovery_codes_remaining ?? 0 })}
          </span>
        )}
      </div>

      {recoveryCodes && (
        <div style={{ border: '1px solid var(--color-border-strong)', borderRadius: 'var(--radius-md)', padding: 'var(--space-3)', display: 'grid', gap: 'var(--space-2)' }}>
          <div style={{ fontWeight: 600 }}>{t('adminMfa.recoveryTitle')}</div>
          <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-muted)' }}>{t('adminMfa.recoveryHelp')}</div>
          <ul
            data-testid="mfa-recovery-codes"
            style={{ margin: 0, padding: 0, listStyle: 'none', display: 'grid', gridTemplateColumns: 'repeat(2, max-content)', gap: '4px 24px', fontFamily: 'var(--font-mono)' }}
          >
            {recoveryCodes.map((c) => <li key={c}>{c}</li>)}
          </ul>
          <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
            <Button size="sm" type="button" onClick={onCopyCodes}>{copied ? t('adminMfa.copied') : t('adminMfa.copyCodes')}</Button>
            <Button size="sm" type="button" onClick={onDownloadCodes}>{t('adminMfa.downloadCodes')}</Button>
            <div style={{ flex: 1 }} />
            <Button size="sm" type="button" variant="primary" onClick={() => setRecoveryCodes(null)}>{t('adminMfa.recoverySaved')}</Button>
          </div>
        </div>
      )}

      {!enabled && !setup && (
        mfa && !mfa.available ? (
          <Notice error>{t('adminMfa.unavailable')}</Notice>
        ) : (
          <form onSubmit={onStart} style={{ display: 'grid', gap: 'var(--space-3)' }}>
            <LockFieldset locked={busy} style={{ display: 'grid', gap: 'var(--space-3)' }}>
              <FormField label={t('admin.currentPasswordConfirm')}>
                <Input
                  type="password"
                  name="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                />
              </FormField>
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                <Button type="submit" variant="primary" loading={busy}>{t('adminMfa.enable')}</Button>
              </div>
            </LockFieldset>
          </form>
        )
      )}

      {!enabled && setup && (
        <form onSubmit={onConfirm} style={{ display: 'grid', gap: 'var(--space-3)' }}>
          <LockFieldset locked={busy} style={{ display: 'grid', gap: 'var(--space-3)' }}>
            <div style={{ fontSize: 'var(--font-size-sm)' }}>{t('adminMfa.setupStep1')}</div>
            <div style={{ display: 'flex', justifyContent: 'center' }}>
              {qr
                ? <img src={qr} width={192} height={192} alt={t('adminMfa.qrAlt')} style={{ background: '#fff', borderRadius: 'var(--radius-md)' }} />
                : <div style={{ width: 192, height: 192 }} />}
            </div>
            <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-muted)' }}>
              {t('adminMfa.manualEntry')}
              <div data-testid="mfa-secret" style={{ fontFamily: 'var(--font-mono)', wordBreak: 'break-all', color: 'var(--color-text)', marginTop: 4 }}>
                {setup.secret.replace(/(.{4})/g, '$1 ').trim()}
              </div>
            </div>
            <FormField label={t('adminMfa.setupStep2')}>
              <Input
                type="text"
                name="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                inputMode="numeric"
                autoComplete="one-time-code"
                placeholder="123456"
                maxLength={8}
              />
            </FormField>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--space-2)' }}>
              <Button type="button" variant="ghost" onClick={onCancelSetup}>{t('adminMfa.cancel')}</Button>
              <Button type="submit" variant="primary" loading={busy}>{t('adminMfa.confirm')}</Button>
            </div>
          </LockFieldset>
        </form>
      )}

      {enabled && (
        <form onSubmit={(e) => e.preventDefault()} style={{ display: 'grid', gap: 'var(--space-3)' }}>
          <LockFieldset locked={busy} style={{ display: 'grid', gap: 'var(--space-3)' }}>
            <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-muted)' }}>{t('adminMfa.manageHelp')}</div>
            {noCodesLeft && <Notice error>{t('adminMfa.noCodesLeftHelp')}</Notice>}
            <FormField label={t('admin.currentPassword')}>
              <Input
                type="password"
                name="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </FormField>
            <FormField label={t('adminMfa.codeOrRecoveryLabel')}>
              <Input
                type="text"
                name="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                autoComplete="one-time-code"
                maxLength={16}
              />
            </FormField>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
              <Button type="button" onClick={() => onManage('regenerate')} disabled={busy}>{t('adminMfa.regenerate')}</Button>
              <Button type="button" variant="danger" onClick={() => onManage('disable')} disabled={busy}>{t('adminMfa.disable')}</Button>
            </div>
          </LockFieldset>
        </form>
      )}

      {msg && <Notice error={msg.error}>{msg.text}</Notice>}
    </div>
  )
}
