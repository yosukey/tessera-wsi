import React, { useEffect, useRef } from 'react'
import { Link, useNavigate } from 'react-router'
import { adminMe, clearAdminSession, isAbortError, isUnauthorizedError, type AdminMe } from '../../lib/api'
import { Button } from '../ui'
import { logger } from '../../lib/logger'
import { useI18n } from '../../lib/i18n'
import LanguageSwitcher from '../LanguageSwitcher'

// Shell of the full-page admin views opened from the dashboard (analytics,
// audit history, error logs): a sticky title bar with the way back, the
// language switcher and logout, plus the access check the page needs.
export default function AdminSubPage({
  title,
  canView,
  children,
}: {
  title: string
  // Whether the signed-in staff member may see this page; anyone else is sent
  // back to the dashboard.
  canView: (me: AdminMe) => boolean
  children: React.ReactNode
}) {
  const nav = useNavigate()
  const { t } = useI18n()
  // Read through a ref so the probe runs once per mount even when the caller
  // passes a fresh inline function on every render.
  const canViewRef = useRef(canView)
  canViewRef.current = canView

  useEffect(() => {
    const ctrl = new AbortController()
    adminMe({ signal: ctrl.signal })
      .then((me) => {
        // Both redirects replace this entry, so Back from where they lead
        // doesn't come back here only to be redirected again.
        if (!canViewRef.current(me)) nav('/admin/dashboard', { replace: true })
      })
      .catch((err) => {
        if (isAbortError(err)) return
        // Only a genuine auth rejection returns to the login screen; a
        // transient failure (429 / 500 / network blip) must not evict an admin
        // holding a valid token. Mirrors AdminDashboard.
        if (isUnauthorizedError(err)) { nav('/admin', { replace: true }); return }
        logger.error('adminMe failed:', err)
      })
    return () => ctrl.abort()
  }, [nav])

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
            {title}
          </h2>
          <div style={{ flex: 1 }} />
          <Link to="/admin/dashboard" style={{ fontSize: 'var(--font-size-sm)' }}>
            {t('admin.dashboard')}
          </Link>
          <LanguageSwitcher />
          <Button size="sm" variant="ghost" onClick={onLogout}>{t('admin.logout')}</Button>
        </div>
        {children}
      </div>
    </div>
  )
}
