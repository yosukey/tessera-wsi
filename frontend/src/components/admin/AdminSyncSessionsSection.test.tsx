// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminListAllSyncSessions: vi.fn(),
  endSyncSession: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../lib/i18n'
import type { AdminSyncSessionInfo } from '../../lib/api'
import AdminSyncSessionsSection from './AdminSyncSessionsSection'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.resetAllMocks()
})

const session = (id: string, slide: string): AdminSyncSessionInfo => ({
  session_id: id, slide_id: 1, slide_title: slide, slide_catalog_id: null, owner_user_id: 1,
  owner_username: 'admin', owner_role: 'admin', presenter_connected: true, follower_count: 0,
  created_at: Math.floor(Date.now() / 1000), grace_remaining: null,
})

describe('AdminSyncSessionsSection', () => {
  it('takes one End at a time, every row waiting for it', async () => {
    // Another row's End must not be ignored without a word while one is out.
    api.adminListAllSyncSessions.mockResolvedValue([session('s-1', 'Liver'), session('s-2', 'Kidney')])
    api.endSyncSession.mockReturnValue(new Promise(() => {}))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<I18nProvider><AdminSyncSessionsSection /></I18nProvider>)
    await waitFor(() => expect(screen.getAllByRole('button', { name: /^End/ })).toHaveLength(2))
    const ends = screen.getAllByRole('button', { name: /^End/ }) as HTMLButtonElement[]
    fireEvent.click(ends[0])
    await waitFor(() => expect(ends.every((b) => b.disabled)).toBe(true))
    expect(api.endSyncSession).toHaveBeenCalledTimes(1)
  })
})
