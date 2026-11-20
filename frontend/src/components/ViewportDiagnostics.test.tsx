// @vitest-environment jsdom
import React from 'react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ViewportDiagnostics from './ViewportDiagnostics'

let go: (to: string) => void = () => {}
function Nav() {
  go = useNavigate()
  return null
}

function mount(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Nav />
      <Routes><Route path="*" element={<ViewportDiagnostics />} /></Routes>
    </MemoryRouter>,
  )
}
const panel = () => screen.queryByTestId('viewport-diagnostics')

describe('ViewportDiagnostics', () => {
  beforeEach(() => {
    sessionStorage.clear()
    // jsdom has no matchMedia.
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }))
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows nothing unless asked for', () => {
    mount('/admin/dashboard')
    expect(panel()).toBeNull()
  })

  it('turns on with ?vpdebug=1, stays on across navigation, and off with ?vpdebug=0', () => {
    mount('/slides/s1?vpdebug=1')
    expect(panel()).not.toBeNull()
    expect(panel()!.textContent).toContain('meta ')
    expect(panel()!.textContent).toContain('layout(inner) ')
    act(() => go('/admin/dashboard'))
    expect(panel()).not.toBeNull()
    act(() => go('/admin/dashboard?vpdebug=0'))
    expect(panel()).toBeNull()
    act(() => go('/'))
    expect(panel()).toBeNull()
  })

  it('logs viewport events as they arrive', () => {
    mount('/?vpdebug=1')
    act(() => { window.dispatchEvent(new Event('orientationchange')) })
    act(() => { window.dispatchEvent(new Event('resize')) })
    const text = panel()!.textContent!
    expect(text).toContain('orientationchange: inner ')
    expect(text).toContain('resize: inner ')
  })
})
