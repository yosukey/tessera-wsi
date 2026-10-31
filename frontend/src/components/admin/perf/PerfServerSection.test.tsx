// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminPerfStartRun: vi.fn(),
}))
vi.mock('../../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../../lib/api')>('../../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../../lib/i18n'
import PerfServerSection from './PerfServerSection'

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('PerfServerSection', () => {
  it('locks the form while the start request is out', async () => {
    // It sends what the form held at the click, and its answer replaces the
    // form with the run.
    api.adminPerfStartRun.mockReturnValue(new Promise(() => {}))
    render(
      <I18nProvider>
        <PerfServerSection isAdmin slides={[]} running={null} pending={false} disabled={false} onStarted={vi.fn()} onConflict={vi.fn()} />
      </I18nProvider>,
    )
    const start = screen.getByRole('button', { name: 'Run server test' })
    const cold = screen.getByRole('checkbox')
    expect(cold.closest('fieldset')!.disabled).toBe(false)
    fireEvent.click(start)
    await waitFor(() => expect(cold.closest('fieldset')!.disabled).toBe(true))
    expect(api.adminPerfStartRun).toHaveBeenCalledTimes(1)
  })
})
