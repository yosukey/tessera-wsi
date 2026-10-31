import React from 'react'
import { useI18n } from '../../lib/i18n'

export default function TourBadge() {
  const { t } = useI18n()
  return (
    <span style={{
      display: 'inline-block',
      fontSize: 10,
      fontWeight: 700,
      letterSpacing: '0.08em',
      textTransform: 'uppercase',
      color: 'var(--color-brand-700)',
      background: 'var(--color-brand-50)',
      padding: '2px 8px',
      borderRadius: 'var(--radius-pill)',
    }}>
      {t('index.tourBadge')}
    </span>
  )
}
