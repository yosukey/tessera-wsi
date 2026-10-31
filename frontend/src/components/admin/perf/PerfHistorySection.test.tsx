// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminPerfDeleteRun: vi.fn(),
}))
vi.mock('../../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../../lib/api')>('../../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../../lib/i18n'
import type { PerfRunSummary } from '../../../lib/api'
import PerfHistorySection from './PerfHistorySection'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.resetAllMocks()
})

const run = (id: number): PerfRunSummary => ({
  id, kind: 'server', state: 'succeeded', created_at: '2026-10-30T00:00:00Z', finished_at: '2026-10-30T00:01:00Z',
  actor: 'admin', params: { slides: [] }, progress: null, cancel_requested: false, error: null, summary: null,
})

describe('PerfHistorySection', () => {
  it('takes one run delete at a time', async () => {
    // A second click would send a second DELETE for the run just removed.
    api.adminPerfDeleteRun.mockReturnValue(new Promise(() => {}))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(
      <I18nProvider>
        <PerfHistorySection runs={[run(1), run(2)]} isAdmin selectedId={null} onSelect={vi.fn()} onDeleted={vi.fn()} />
      </I18nProvider>,
    )
    const del1 = screen.getByRole('button', { name: 'Delete run #1' }) as HTMLButtonElement
    const del2 = screen.getByRole('button', { name: 'Delete run #2' }) as HTMLButtonElement
    fireEvent.click(del1)
    await waitFor(() => expect(del1.disabled && del2.disabled).toBe(true))
    expect(api.adminPerfDeleteRun).toHaveBeenCalledTimes(1)
  })
})
