import { describe, expect, it } from 'vitest'
import en from '../i18n/en'
import { transferProgressText } from './transferProgress'

// The English catalogue, interpolated as the app does.
const t = (k: string, p: Record<string, string | number> = {}) => {
  const v = (en as Record<string, unknown>)[k]
  return typeof v === 'function' ? (v as (q: typeof p) => string)(p) : (v as string) ?? k
}

describe('transferProgressText', () => {
  it('shows the bytes an export has written', () => {
    expect(transferProgressText(t, { phase: 'writing_wsi', bytes_written: 3 * 1024 * 1024 }))
      .toBe('Writing the bundle… 3.0 MB')
    expect(transferProgressText(t, { phase: 'finalizing', bytes_written: 2048 }))
      .toBe('Finishing the bundle (2 KB)…')
  })

  it('shows how much of a received bundle is copied into staging', () => {
    expect(transferProgressText(t, { phase: 'copying', bytes_copied: 256, total_bytes: 1024 }))
      .toBe('Receiving the bundle on the server… 256 B of 1 KB (25%)')
    // Nothing known yet.
    expect(transferProgressText(t, { phase: 'copying' })).toBeNull()
  })

  it('names the unpacking and checking steps', () => {
    // The copy's byte counts linger in the merged progress; the phase rules.
    expect(transferProgressText(t, { phase: 'extracting', bytes_copied: 1, total_bytes: 1 }))
      .toBe('Unpacking the bundle…')
    expect(transferProgressText(t, { phase: 'checking' })).toBe('Checking the bundle…')
  })

  it('counts the slides an apply and the attachments a retry have done', () => {
    expect(transferProgressText(t, { phase: 'importing', done: 2, total: 5 }))
      .toBe('Importing… 2 of 5 slide(s) done')
    expect(transferProgressText(t, { phase: 'copying_assets', done: 0, total: 4 }))
      .toBe('Copying attachments… 0 of 4 done')
  })

  it('has nothing to say while queued, when done, or for an unknown phase', () => {
    for (const p of [{ phase: 'queued' }, { phase: 'done' }, { phase: 'later' }, {}]) {
      expect(transferProgressText(t, p)).toBeNull()
    }
    expect(transferProgressText(t, null)).toBeNull()
  })
})
