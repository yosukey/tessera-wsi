import { describe, expect, it } from 'vitest'
import { clientSafeFilename } from './safeFilename'

// Same expectations as the server's safe_filename tests
// (services/app/tests/test_upload_nonascii_filename.py).
describe('clientSafeFilename', () => {
  it('canonicalises like the server', () => {
    expect(clientSafeFilename('slide 01.svs')).toBe('slide_01.svs')
    expect(clientSafeFilename('')).toBe('file')
    expect(clientSafeFilename('x'.repeat(300) + '.svs')).toBe('x'.repeat(196) + '.svs')
    expect(clientSafeFilename('case12..svs')).toBe('case12.svs')
    expect(clientSafeFilename('a.b..tiff')).toBe('a.b.tiff')
    expect(clientSafeFilename('a....svs')).toBe('a_.svs')
    expect(clientSafeFilename('a..b.svs')).toBe('a_b.svs')
    expect(clientSafeFilename('...svs')).toBe('file.svs')
    expect(clientSafeFilename('.....tiff')).toBe('file.tiff')
  })

  it('cuts an over-long extension only when the name would not fit', () => {
    expect(clientSafeFilename('a.' + 'b'.repeat(300))).toBe('a.' + 'b'.repeat(15))
    expect(clientSafeFilename('s'.repeat(150) + '.' + 'e'.repeat(100))).toBe('s'.repeat(150) + '.' + 'e'.repeat(15))
    expect(clientSafeFilename('report.final-reviewed-version')).toBe('report.final-reviewed-version')
  })
})
