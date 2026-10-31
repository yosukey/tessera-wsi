// @vitest-environment jsdom
import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../lib/i18n'
import SyncIdleToast from './SyncIdleToast'
import type { IdleNotice } from '../hooks/useSyncPresenterSession'

function show(notice: IdleNotice | null) {
  return render(
    <I18nProvider>
      <SyncIdleToast notice={notice} onDismiss={vi.fn()} />
    </I18nProvider>,
  )
}

describe('SyncIdleToast', () => {
  afterEach(cleanup)

  it('explains an unpublished-slide move and can be dismissed', () => {
    show({ kind: 'unavailable' })
    expect(screen.getByRole('status').textContent).toContain('not published')
    expect(screen.getByLabelText(/close/i)).toBeTruthy()
  })

  it('announces the max-age cap in minutes and can be dismissed', () => {
    show({ kind: 'maxAgeWarning', seconds: 290 })
    expect(screen.getByRole('status').textContent).toContain('about 5 min')
    expect(screen.getByLabelText(/close/i)).toBeTruthy()
  })

  it('uses a distinct message when the age cap ended the session', () => {
    show({ kind: 'ended', reason: 'max_age' })
    expect(screen.getByRole('alert').textContent).toContain('maximum session duration')
  })

  it('keeps the idle countdown undismissable', () => {
    show({ kind: 'warning', seconds: 60 })
    expect(screen.queryByLabelText(/close/i)).toBeNull()
  })
})
