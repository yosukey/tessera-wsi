// @vitest-environment jsdom
import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('qrcode', () => ({ default: { toCanvas: () => Promise.resolve() } }))

import { I18nProvider } from '../lib/i18n'
import type { AudienceFrame, FollowerInfo } from '../lib/sync'
import SyncPresenterSidebar from './SyncPresenterSidebar'

const session = {
  session_id: 's1',
  slide_id: 1,
  slide_public_id: 'p1',
  follower_url: 'https://wsi.example.org/slides/p1?sync=s1',
}

function follower(over: Partial<FollowerInfo>): FollowerInfo {
  return {
    id: 'f1', name: 'Hanako', ip: '203.0.113.7',
    ua: 'Mozilla/5.0 (Windows NT 10.0) Chrome/128.0 Safari/537.36',
    hostname: null, joined_at: Date.now() / 1000, visible: true, vw: null, vh: null,
    ...over,
  }
}

function show(audience: AudienceFrame) {
  return render(
    <I18nProvider>
      <SyncPresenterSidebar session={session} audience={audience} connState="open" onStop={vi.fn()} />
    </I18nProvider>,
  )
}

describe('SyncPresenterSidebar audience rows', () => {
  afterEach(cleanup)

  it('shows the address and browser of a follower', () => {
    show({ type: 'audience', count: 1, followers: [follower({})] })
    expect(screen.getByText('203.0.113.7 — Chrome 128 / Windows')).toBeTruthy()
    expect(screen.queryByText(/pseudonym/i)).toBeNull()
  })

  // Analytics privacy mode "hashed": the server sends a pseudonym and no UA.
  it('explains a pseudonymous audience and drops the empty browser part', () => {
    show({
      type: 'audience', count: 1, pseudonymous: true,
      followers: [follower({ ip: '3fa9c2d1e0b4', ua: '', hostname: null })],
    })
    expect(screen.getByText('3fa9c2d1e0b4')).toBeTruthy()
    expect(screen.getByText(/pseudonyms/)).toBeTruthy()
  })
})
