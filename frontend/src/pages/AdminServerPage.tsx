import React, { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { adminMe, clearAdminSession, isAbortError, isUnauthorizedError } from '../lib/api'
import { Button } from '../components/ui'
import { logger } from '../lib/logger'
import { canViewServer } from '../lib/admin/permissions'
import { useI18n } from '../lib/i18n'
import LanguageSwitcher from '../components/LanguageSwitcher'
import AdminServerSection from '../components/admin/AdminServerSection'
import AdminSyncSessionsSection from '../components/admin/AdminSyncSessionsSection'

export default function AdminServerPage() {
  const nav = useNavigate()
  const { t } = useI18n()
  // The sections (server details, the live-session list and its force-end
  // buttons) render only once /me has confirmed this role may see the page.
  const [allowed, setAllowed] = useState(false)
  const [meFailed, setMeFailed] = useState(false)
  const [meReloadKey, setMeReloadKey] = useState(0)

  useEffect(() => {
    const ctrl = new AbortController()
    setMeFailed(false)
    adminMe({ signal: ctrl.signal })
      // Both redirects replace this entry, as AdminDashboard's does.
      .then((me) => {
        if (canViewServer(me)) setAllowed(true)
        else nav('/admin/dashboard', { replace: true })
      })
      .catch((err) => {
        if (isAbortError(err)) return
        // Only a genuine auth rejection returns to the login screen; a
        // transient failure (429 / 500 / network blip) must not evict an admin
        // holding a valid token. Mirrors AdminDashboard. The check can then be
        // retried from here.
        if (isUnauthorizedError(err)) { nav('/admin', { replace: true }); return }
        logger.error('adminMe failed:', err)
        setMeFailed(true)
      })
    return () => ctrl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meReloadKey])

  function onLogout() {
    if (!window.confirm(t('admin.logoutConfirm'))) return
    clearAdminSession()
    nav('/admin')
  }

  return (
    <div style={{
      minHeight: '100vh',
      background: 'var(--color-bg)',
      // Top spacing is owned by the sticky header below (via its paddingTop) so
      // it stays opaque and visible when pinned; the root keeps only the side
      // and bottom insets.
      padding: '0 var(--space-4) var(--space-5)',
    }}>
      <div style={{ maxWidth: 1400, margin: '0 auto' }}>
        {/* Keep the title and back/logout navigation pinned to the top of the
            viewport while scrolling the page content. The opaque background
            covers content scrolling underneath; the vertical padding (rather
            than a bottom margin) keeps that cover gap-free while pinned. */}
        <div style={{
          position: 'sticky',
          top: 0,
          zIndex: 1,
          background: 'var(--color-bg)',
          display: 'flex',
          alignItems: 'center',
          gap: 'var(--space-3)',
          paddingTop: 'var(--space-5)',
          paddingBottom: 'var(--space-4)',
        }}>
          <h2 style={{
            margin: 0,
            fontSize: 'var(--font-size-2xl)',
            fontWeight: 700,
            letterSpacing: '-0.01em',
          }}>
            {t('adminDash.tabServer')}
          </h2>
          <div style={{ flex: 1 }} />
          <Link to="/admin/performance" style={{ fontSize: 'var(--font-size-sm)' }}>
            {t('adminDash.tabPerformance')}
          </Link>
          <Link to="/admin/dashboard" style={{ fontSize: 'var(--font-size-sm)' }}>
            {t('admin.dashboard')}
          </Link>
          <LanguageSwitcher />
          <Button size="sm" variant="ghost" onClick={onLogout}>{t('admin.logout')}</Button>
        </div>
        {allowed ? (
          <>
            <AdminServerSection />
            <AdminSyncSessionsSection />
          </>
        ) : meFailed ? (
          <div role="alert" style={{ fontSize: 'var(--font-size-sm)', display: 'flex', alignItems: 'center', gap: 'var(--space-3)', flexWrap: 'wrap' }}>
            <span style={{ color: 'var(--color-danger-fg)' }}>{t('adminDash.accessCheckFailed')}</span>
            <Button size="sm" variant="secondary" onClick={() => setMeReloadKey((k) => k + 1)}>{t('adminDash.retry')}</Button>
          </div>
        ) : (
          <div style={{ fontSize: 'var(--font-size-sm)', opacity: 0.6 }}>{t('adminDash.loading')}</div>
        )}
      </div>
    </div>
  )
}
