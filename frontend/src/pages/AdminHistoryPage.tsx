import React from 'react'
import { useI18n } from '../lib/i18n'
import { isAdmin } from '../lib/admin/permissions'
import AdminSubPage from '../components/admin/AdminSubPage'
import AdminHistorySection from '../components/admin/AdminHistorySection'

// History exposes login events, user management, and settings changes, so it
// is hard-gated to admins (no role_permissions opt-in).
export default function AdminHistoryPage() {
  const { t } = useI18n()
  return (
    <AdminSubPage title={t('adminDash.tabHistory')} canView={isAdmin}>
      <AdminHistorySection />
    </AdminSubPage>
  )
}
