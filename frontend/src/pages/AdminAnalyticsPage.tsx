import React from 'react'
import { useI18n } from '../lib/i18n'
import { canViewAnalytics } from '../lib/admin/permissions'
import AdminSubPage from '../components/admin/AdminSubPage'
import AdminAnalyticsView from '../components/admin/AdminAnalyticsView'

export default function AdminAnalyticsPage() {
  const { t } = useI18n()
  return (
    <AdminSubPage title={t('adminDash.tabAnalytics')} canView={canViewAnalytics}>
      <AdminAnalyticsView />
    </AdminSubPage>
  )
}
