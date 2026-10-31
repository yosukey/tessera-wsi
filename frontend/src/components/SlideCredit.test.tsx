// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../lib/i18n'
import SlideCredit from './SlideCredit'
import type { SlideCredit as Credit } from '../lib/slideCredit'

let narrow = false
beforeEach(() => {
  localStorage.clear()
  narrow = false
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: narrow && query.includes('max-width'),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function mount(credit: Credit | null) {
  return render(
    <I18nProvider>
      <SlideCredit credit={credit} />
    </I18nProvider>,
  )
}

describe('SlideCredit', () => {
  it('renders nothing for a slide without credit', () => {
    const { container } = mount({ credit_text: '  ', license_id: '', source_url: '' })
    expect(container.innerHTML).toBe('')
    expect(mount(null).container.innerHTML).toBe('')
  })

  it('shows the holder, the linked license and the source link', () => {
    mount({ credit_text: '© 2024 Example Lab', license_id: 'CC-BY-NC-4.0', source_url: 'https://example.org/s/1' })
    expect(screen.getByRole('complementary', { name: 'Image credit' })).toBeTruthy()
    expect(screen.getByText('© 2024 Example Lab')).toBeTruthy()
    const lic = screen.getByRole('link', { name: 'CC BY-NC 4.0' })
    expect(lic.getAttribute('href')).toBe('https://creativecommons.org/licenses/by-nc/4.0/')
    expect(lic.getAttribute('rel')).toContain('license')
    const src = screen.getByRole('link', { name: /Source/ })
    expect(src.getAttribute('href')).toBe('https://example.org/s/1')
    expect(src.getAttribute('rel')).toContain('noopener')
  })

  it('shows a license without a deed as plain text', () => {
    mount({ license_id: 'ARR' })
    expect(screen.getByText('All rights reserved')).toBeTruthy()
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('never renders a non-http(s) source as a link', () => {
    mount({ credit_text: 'X', source_url: 'javascript:alert(1)' })
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('ignores a license id it does not know', () => {
    const { container } = mount({ license_id: 'FUTURE-LICENSE-9' })
    expect(container.innerHTML).toBe('')
  })

  it('starts collapsed on a narrow screen and expands on tap', () => {
    narrow = true
    mount({ credit_text: '© Example Lab', license_id: 'CC0-1.0' })
    expect(screen.queryByText('© Example Lab')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Show image credit' }))
    expect(screen.getByText('© Example Lab')).toBeTruthy()
  })
})
