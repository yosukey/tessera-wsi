import React from 'react'
import { useI18n } from '../lib/i18n'
import { isAdmin } from '../lib/admin/permissions'
import AdminSubPage from '../components/admin/AdminSubPage'
import AdminLogsSection from '../components/admin/AdminLogsSection'

// Error logs can expose request paths, identifiers, and stack traces, so the
// surface is hard-gated to admins (no role_permissions opt-in) — same as the
// audit history page.
export default function AdminLogsPage() {
  const { t } = useI18n()
  return (
    <AdminSubPage title={t('adminDash.tabLogs')} canView={isAdmin}>
      <AdminLogsSection />
    </AdminSubPage>
  )
}
