// @vitest-environment jsdom
import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import PhaseStatus from './PhaseStatus'

afterEach(cleanup)

describe('PhaseStatus', () => {
  it('announces a new phase, not every update of the visible line', () => {
    const { rerender } = render(<PhaseStatus text="Writing the bundle… 1 MB" phase="writing_wsi" />)
    const live = screen.getByRole('status')
    expect(live.textContent).toBe('Writing the bundle… 1 MB')

    rerender(<PhaseStatus text="Writing the bundle… 2 MB" phase="writing_wsi" />)
    // Shown, but the live region did not change: nothing new is read out.
    expect(screen.getByText('Writing the bundle… 2 MB').getAttribute('aria-live')).toBe('off')
    expect(live.textContent).toBe('Writing the bundle… 1 MB')

    rerender(<PhaseStatus text="Finishing the bundle (3 MB)…" phase="finalizing" />)
    expect(live.textContent).toBe('Finishing the bundle (3 MB)…')
  })
})
