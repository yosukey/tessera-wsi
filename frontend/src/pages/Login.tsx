import React, { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { Button, FormField, Input, Panel } from '../components/ui'
import LanguageSwitcher from '../components/LanguageSwitcher'
import { useI18n } from '../lib/i18n'
import { sanitizeNext } from '../lib/nextPath'
import { appLogin, getAuthState, isRateLimitError, AuthMode } from '../lib/api'

export default function Login() {
  const nav = useNavigate()
  const { t } = useI18n()
  // Where to go after signing in (set by useAuthGate); anything that isn't a
  // same-origin relative path falls back to the home page.
  const [params] = useSearchParams()
  const next = sanitizeNext(params.get('next')) ?? '/'
  const [mode, setMode] = useState<AuthMode | null>(null)
  // Operator-configured overrides for the message and button label. Empty
  // strings fall back to the per-mode built-in i18n defaults below.
  const [customMessage, setCustomMessage] = useState('')
  const [customButtonLabel, setCustomButtonLabel] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  // Set when /api/auth/state could not be read. Never fall back to showing the
  // "auth is off" hint for that: the server may well be enforcing a login.
  const [stateFailed, setStateFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    setStateFailed(false)
    getAuthState()
      .then(s => {
        if (s.authenticated) {
          nav(next, { replace: true })
          return
        }
        setMode(s.mode)
        setCustomMessage(s.login_message || '')
        setCustomButtonLabel(s.login_button_label || '')
      })
      .catch(() => setStateFailed(true))
  }, [nav, next, attempt])

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setErr('')
    setLoading(true)
    try {
      await appLogin(username, password)
      nav(next, { replace: true })
    } catch (e: any) {
      // Not e.message: that is the server's English detail, or the internal
      // "rate-limited" code for a 429 (login throttle / nginx limit).
      setErr(isRateLimitError(e) ? t('common.rateLimited') : t('login.failed'))
    } finally {
      setLoading(false)
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
            {t('login.title')}
          </h2>

          {stateFailed && (
            <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
              <div role="alert" style={{
                padding: '8px 12px',
                borderRadius: 'var(--radius-md)',
                background: 'var(--color-danger-bg)',
                border: '1px solid var(--color-danger-border)',
                color: 'var(--color-danger-fg)',
                fontSize: 'var(--font-size-sm)',
              }}>{t('login.stateError')}</div>
              <Button type="button" variant="primary" fullWidth onClick={() => setAttempt(n => n + 1)}>
                {t('login.retry')}
              </Button>
            </div>
          )}

          {!stateFailed && mode === null && (
            <div style={{ fontSize: 'var(--font-size-sm)', opacity: 0.7 }}>{t('login.loading')}</div>
          )}

          {!stateFailed && mode === 'none' && (
            <div style={{ fontSize: 'var(--font-size-sm)' }}>{customMessage || t('login.noneHint')}</div>
          )}

          {mode === 'basic' && (
            <div style={{ fontSize: 'var(--font-size-sm)' }}>{customMessage || t('login.basicHint')}</div>
          )}

          {mode === 'ldap' && (
            <form onSubmit={onSubmit} style={{ display: 'grid', gap: 'var(--space-3)' }}>
              {customMessage && (
                <div style={{ fontSize: 'var(--font-size-sm)', opacity: 0.8 }}>{customMessage}</div>
              )}
              <FormField label={t('login.username')}>
                <Input
                  name="username"
                  value={username}
                  onChange={e => setUsername(e.target.value)}
                  autoComplete="username"
                  required
                />
              </FormField>
              <FormField label={t('login.password')}>
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
                {customButtonLabel || t('login.signIn')}
              </Button>
              {err && (
                <div style={{
                  padding: '8px 12px',
                  borderRadius: 'var(--radius-md)',
                  background: 'var(--color-danger-bg)',
                  border: '1px solid var(--color-danger-border)',
                  color: 'var(--color-danger-fg)',
                  fontSize: 'var(--font-size-sm)',
                }}>{err}</div>
              )}
            </form>
          )}

          {mode === 'google_oauth' && (
            <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
              <div style={{ fontSize: 'var(--font-size-sm)', opacity: 0.8 }}>
                {customMessage || t('login.googleHint')}
              </div>
              <a href={next === '/' ? '/api/auth/oauth/start' : `/api/auth/oauth/start?next=${encodeURIComponent(next)}`} style={{ textDecoration: 'none' }}>
                <Button type="button" variant="primary" fullWidth>
                  {customButtonLabel || t('login.signInWithGoogle')}
                </Button>
              </a>
            </div>
          )}
        </Panel>

        <div style={{
          marginTop: 'var(--space-4)',
          textAlign: 'center',
          fontSize: 'var(--font-size-sm)',
        }}>
          <LanguageSwitcher />
        </div>
      </div>
    </div>
  )
}
