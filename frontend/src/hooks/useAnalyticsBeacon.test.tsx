// @vitest-environment jsdom
import React from 'react'
import { cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ recordPageView: vi.fn() }))
vi.mock('../lib/api/analytics', () => api)

import { useAnalyticsBeacon } from './useAnalyticsBeacon'

function Probe() {
  useAnalyticsBeacon()
  return null
}

afterEach(() => {
  cleanup()
  api.recordPageView.mockReset()
})

function pageshow(persisted: boolean) {
  window.dispatchEvent(Object.assign(new Event('pageshow'), { persisted }))
}

describe('useAnalyticsBeacon', () => {
  it('records a fresh page view when the page is restored from the bfcache', () => {
    render(<MemoryRouter initialEntries={['/slides/abc']}><Probe /></MemoryRouter>)
    expect(api.recordPageView).toHaveBeenCalledTimes(1)
    // Inside the dedupe window on purpose: a restore is a real visit.
    pageshow(true)
    expect(api.recordPageView).toHaveBeenCalledTimes(2)
    expect(api.recordPageView.mock.calls[1][0]).toEqual({ kind: 'viewer', slide_public_id: 'abc' })
  })

  it('ignores an ordinary pageshow and skipped routes', () => {
    render(<MemoryRouter initialEntries={['/admin/analytics']}><Probe /></MemoryRouter>)
    pageshow(false)
    pageshow(true)
    expect(api.recordPageView).not.toHaveBeenCalled()
  })
})
