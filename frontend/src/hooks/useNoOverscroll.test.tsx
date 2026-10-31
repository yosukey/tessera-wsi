// @vitest-environment jsdom
import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { NO_OVERSCROLL_CLASS, useNoOverscroll } from './useNoOverscroll'

const hasClass = () => document.documentElement.classList.contains(NO_OVERSCROLL_CLASS)

describe('useNoOverscroll', () => {
  it('marks <html> while mounted and clears it on unmount', () => {
    const { unmount } = renderHook(() => useNoOverscroll())
    expect(hasClass()).toBe(true)
    unmount()
    expect(hasClass()).toBe(false)
  })

  it('keeps the class while another page still holds it', () => {
    const a = renderHook(() => useNoOverscroll())
    const b = renderHook(() => useNoOverscroll())
    a.unmount()
    expect(hasClass()).toBe(true)
    b.unmount()
    expect(hasClass()).toBe(false)
  })
})
