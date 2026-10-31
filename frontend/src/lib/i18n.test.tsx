// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { I18nProvider, useI18n, type Locale } from './i18n'
import en from './i18n/en'
import ja from './i18n/ja'

afterEach(() => {
  cleanup()
  localStorage.clear()
  document.documentElement.lang = 'en'
})

describe('I18nProvider', () => {
  it('keeps <html lang> on the active locale', async () => {
    localStorage.setItem('wsi-locale', 'ja')
    let setLocale: (l: Locale) => void = () => {}
    function Probe() {
      setLocale = useI18n().setLocale
      return null
    }
    render(<I18nProvider><Probe /></I18nProvider>)
    await waitFor(() => expect(document.documentElement.lang).toBe('ja'))
    act(() => setLocale('en'))
    expect(document.documentElement.lang).toBe('en')
    expect(localStorage.getItem('wsi-locale')).toBe('en')
  })
})

// Copy checks for the access-analytics strings that describe irreversible or
// server-side behaviour, where a wording slip misinforms the admin.
describe('analytics copy', () => {
  // A template's fixed wording, with any parameter it is not given left blank.
  const text = (v: unknown, params: Record<string, string> = {}): string => {
    if (typeof v !== 'function') return String(v)
    try { return String(v(new Proxy(params, { get: (o, k) => (k in o ? o[k as string] : '') }))) } catch { return '' }
  }

  it('names every column pseudonymizing rewrites, in both locales', () => {
    // pseudonymize_until also hashes client_ip and blanks user_agent.
    expect(en['analytics.pseudonymizeConfirm']).toMatch(/IP address/)
    expect(en['analytics.pseudonymizeConfirm']).toMatch(/User-Agent/)
    expect(ja['analytics.pseudonymizeConfirm']).toMatch(/IP アドレス/)
    expect(ja['analytics.pseudonymizeConfirm']).toMatch(/User-Agent/)
  })

  it('does not ask for Pseudonymous mode where Off is accepted too', () => {
    // The server refuses pseudonymizing only while recording is Identified
    // ("plain"); with recording Off it converts what was kept.
    expect(en['analytics.pseudonymizeConfirm']).toMatch(/\(or Off\)/)
    expect(ja['analytics.pseudonymizeConfirm']).toMatch(/（または「記録しない」）/)
  })

  it('spells pseudonymize one way in English', () => {
    const bad = Object.entries(en).filter(([, v]) => /pseudonymis/i.test(text(v)))
    expect(bad.map(([k]) => k)).toEqual([])
  })

  it('words the Japanese analytics messages consistently', () => {
    expect(text(ja['analytics.clearTzNote'], { tz: 'Asia/Tokyo' })).toBe('アクセス解析のタイムゾーン：Asia/Tokyo')
    const mixed = Object.entries(ja).filter(([, v]) => text(v).includes('何も削除していません'))
    expect(mixed.map(([k]) => k)).toEqual([])
  })
})
