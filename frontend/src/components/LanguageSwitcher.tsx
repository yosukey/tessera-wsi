import React from 'react'
import { Select } from './ui'
import { useI18n } from '../lib/i18n'

export default function LanguageSwitcher() {
  const { locale, setLocale, t } = useI18n()

  return (
    <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <span style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-subtle)' }}>{t('common.language')}</span>
      <Select size="sm" value={locale} onChange={(e) => setLocale(e.target.value as 'en' | 'ja')}>
        <option value="en">{t('lang.en')}</option>
        <option value="ja">{t('lang.ja')}</option>
      </Select>
    </label>
  )
}
