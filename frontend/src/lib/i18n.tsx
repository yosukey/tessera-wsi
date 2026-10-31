import React, { createContext, useContext, useEffect, useMemo, useState } from 'react'
import enBundle from './i18n/en'
import { STORAGE_KEYS } from './storageKeys'
import { safeGetItem, safeSetItem } from './safeStorage'

export type Locale = 'en' | 'ja'

type MessageValue = string | ((params: Record<string, string | number>) => string)
type Messages = Record<string, MessageValue>

// Widened to the string-indexed shape: `t` takes any string key (some are
// built at runtime) and falls back to the bare key on a miss.
const enMessages: Messages = enBundle

// English is shipped synchronously in the main chunk so the very first
// render never falls through to bare keys (e.g. "home.title") on a cold
// load. Japanese is still lazy-loaded — it's only fetched when the active
// locale is `ja`, and the English fallback covers the brief gap before
// the bundle resolves.
function loadLocale(locale: Locale): Promise<Messages> {
  if (locale === 'ja') return import('./i18n/ja').then((m) => m.default)
  return Promise.resolve(enMessages)
}

function detectLocale(): Locale {
  if (typeof window === 'undefined') return 'en'
  const saved = safeGetItem(STORAGE_KEYS.locale)
  if (saved === 'ja' || saved === 'en') return saved
  // `navigator.languages` is undefined on some older Safari / embedded
  // WebViews; calling .some() on it there throws, and because this runs inside
  // the root provider's useState initializer an uncaught throw white-screens
  // the whole SPA (there is no ErrorBoundary above it). Fall back to the
  // singular `navigator.language`, then to 'en'.
  const langs: string[] = Array.isArray(navigator.languages) && navigator.languages.length
    ? (navigator.languages as string[])
    : (navigator.language ? [navigator.language] : [])
  if (langs.some((lang) => String(lang).toLowerCase().startsWith('ja'))) return 'ja'
  return 'en'
}

type I18nContextValue = {
  locale: Locale
  setLocale: (next: Locale) => void
  t: (key: string, params?: Record<string, string | number>) => string
}

const I18nContext = createContext<I18nContextValue | null>(null)

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => detectLocale())
  // Seed `msgs` with the English bundle synchronously so the first paint
  // already has translations. When the active locale is Japanese, this
  // is overwritten once that bundle resolves; until then the user sees
  // English, which is far better UX than raw keys flashing on screen.
  const [msgs, setMsgs] = useState<Messages>(enMessages)

  // Keep <html lang> on the active locale (index.html ships "en"), so screen
  // readers pick the right voice and the browser the right fonts / hyphenation.
  useEffect(() => {
    if (typeof document !== 'undefined') document.documentElement.lang = locale
  }, [locale])

  useEffect(() => {
    if (locale === 'en') {
      // Main bundle already has the English messages — no dynamic import.
      setMsgs(enMessages)
      return
    }
    let cancelled = false
    loadLocale(locale).then((m) => {
      if (!cancelled) setMsgs(m)
    })
    return () => { cancelled = true }
  }, [locale])

  const value = useMemo<I18nContextValue>(() => ({
    locale,
    setLocale: (next) => {
      setLocaleState(next)
      safeSetItem(STORAGE_KEYS.locale, next)
    },
    t: (key, params = {}) => {
      // Always fall through to the English bundle if the active-locale
      // lookup misses (e.g. a key only added to `en.ts`). Returning the
      // bare key is a last resort and indicates a missing translation.
      const localized = msgs[key] ?? enMessages[key]
      if (!localized) return key
      return typeof localized === 'function' ? localized(params) : localized
    },
  }), [locale, msgs])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n() {
  const ctx = useContext(I18nContext)
  if (!ctx) throw new Error('useI18n must be used within I18nProvider')
  return ctx
}
