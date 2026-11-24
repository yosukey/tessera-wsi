// @vitest-environment jsdom
import React from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// The lazily loaded Japanese chunk fails to load (offline, or replaced by a
// redeploy). Kept in its own file: the mock applies to the whole module graph.
vi.mock('./i18n/ja', () => { throw new Error('chunk load failed') })
const warn = vi.hoisted(() => vi.fn())
vi.mock('./logger', () => ({ logger: { warn, error: vi.fn(), info: vi.fn(), debug: vi.fn() } }))

import { I18nProvider, useI18n } from './i18n'

afterEach(() => {
  cleanup()
  localStorage.clear()
})

describe('I18nProvider when a locale chunk fails to load', () => {
  it('keeps the English messages and handles the rejection', async () => {
    localStorage.setItem('wsi-locale', 'ja')
    function Probe() {
      return <span>{useI18n().t('login.title')}</span>
    }
    render(<I18nProvider><Probe /></I18nProvider>)
    // Handled (logged), not left as an unhandled rejection, which vitest
    // would report as an error for this file.
    await waitFor(() => expect(warn).toHaveBeenCalled())
    expect(screen.getByText('Sign in')).toBeTruthy()
  })
})
