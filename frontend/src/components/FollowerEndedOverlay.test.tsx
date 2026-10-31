// @vitest-environment jsdom
import React from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import FollowerEndedOverlay, { followerOverlayContent } from './FollowerEndedOverlay'
import { I18nProvider } from '../lib/i18n'
import en from '../lib/i18n/en'

const t = ((key: string) => (en as Record<string, unknown>)[key] as string) as never
const text = (key: string) => (en as Record<string, unknown>)[key] as string

describe('the follower end screen', () => {
  it.each([
    ['presenter_idle', 'sync.sessionEndedIdle'],
    ['max_age', 'sync.sessionEndedMaxAge'],
    ['ended_by_owner', 'sync.sessionEndedByPresenter'],
    ['presenter_gone', 'sync.sessionEndedPresenterGone'],
    ['superseded', 'sync.sessionSuperseded'],
    ['ended', 'sync.sessionEnded'],
    [null, 'sync.sessionEnded'],
  ])('an end for %s reads as %s', (reason, key) => {
    expect(followerOverlayContent('ended', reason, null, t, () => {}).message).toBe(text(key))
  })

  it.each([
    ['notfound', 'sync.sessionNotFound'],
    ['error', 'sync.sessionPeekError'],
    ['full', 'sync.roomFull'],
    ['unauthorized', 'sync.sessionUnauthorized'],
    ['forbidden', 'sync.accessBlocked'],
  ] as const)('%s reads as %s', (status, key) => {
    expect(followerOverlayContent(status, null, null, t, () => {}).message).toBe(text(key))
  })

  it('a failed session check offers a retry that runs it again', () => {
    const retry = vi.fn()
    const content = followerOverlayContent('error', null, null, t, retry)
    render(<I18nProvider><FollowerEndedOverlay {...content} /></I18nProvider>)
    fireEvent.click(screen.getByRole('button', { name: text('sync.retry') }))
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it('a superseded end links to the session that replaced it', () => {
    const content = followerOverlayContent(
      'ended', 'superseded', { sid: 'N W', slide_public_id: 'P9' }, t, () => {},
    )
    render(<I18nProvider><FollowerEndedOverlay {...content} /></I18nProvider>)
    expect(screen.getByRole('link', { name: text('sync.joinNextSession') }).getAttribute('href'))
      .toBe('/slides/P9?sync=N%20W')
  })

  it('offers nothing but "back to top" otherwise', () => {
    expect(followerOverlayContent('ended', 'ended_by_owner', null, t, () => {}).action).toBeUndefined()
    expect(followerOverlayContent('notfound', null, null, t, () => {}).action).toBeUndefined()
  })
})

describe('the join-the-new-session offer', () => {
  it('is only for an ended session', () => {
    const next = { sid: 'N', slide_public_id: 'P' }
    expect(followerOverlayContent('notfound', null, next, t, () => {}).action).toBeUndefined()
    expect(followerOverlayContent('ended', 'superseded', next, t, () => {}).action?.href).toBe('/slides/P?sync=N')
  })
})
