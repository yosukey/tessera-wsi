import { describe, expect, it } from 'vitest'
import { ACCEPTED_WSI_EXTENSIONS, displayFilename, exceedsUploadLimit, formatByteLimit, formatBytes, validateMetadata } from './validation'
import type { TFn } from './validation'

// Echo the key and params so assertions can see which check fired.
const t: TFn = (key, params) => (params ? `${key} ${JSON.stringify(params)}` : key)

describe('validateMetadata', () => {
  it('counts characters, not UTF-16 units, as the server does', () => {
    // 200 emoji: 200 characters to the server, 400 UTF-16 units to JS.
    expect(validateMetadata({ title: '🔬'.repeat(200), catalog_id: '' }, {}, t)).toBeNull()
    expect(validateMetadata({ title: '🔬'.repeat(255), catalog_id: '' }, {}, t)).toBeNull()
    expect(validateMetadata({ title: '🔬'.repeat(256), catalog_id: '' }, {}, t))
      .toContain('"max":255,"len":256')
    expect(validateMetadata({}, { stain: ['𠀋'.repeat(255)] }, t)).toBeNull()
    expect(validateMetadata({}, { ['𠀋'.repeat(128)]: ['x'] }, t)).toBeNull()
  })

  it('checks the title and catalog ID trimmed, as the server strips them', () => {
    expect(validateMetadata({ title: `${'a'.repeat(255)}   `, catalog_id: '' }, {}, t)).toBeNull()
    expect(validateMetadata({ title: 'Liver\n', catalog_id: ' C-1\t' }, {}, t)).toBeNull()
    expect(validateMetadata({ title: 'Li\nver', catalog_id: '' }, {}, t)).toContain('validationCtrlChar')
  })

  it('labels the field the way the editor heading does', () => {
    expect(validateMetadata({ catalog_id: 'x'.repeat(129), title: '' }, {}, t))
      .toBe('adminDash.validationMaxLen {"label":"adminDash.fieldCatalogId","max":128,"len":129}')
  })

  it('does not re-check values unchanged since the form was seeded', () => {
    const stored = {
      fields: { title: 'Bad\u0001title', catalog_id: 'x'.repeat(200) },
      attrs: { 'bad,key': ['v\u0002'] },
    }
    const fields = { title: 'Bad\u0001title', catalog_id: 'x'.repeat(200) }
    const attrs = { 'bad,key': ['v\u0002'] }
    expect(validateMetadata(fields, attrs, t)).not.toBeNull()
    expect(validateMetadata(fields, attrs, t, stored)).toBeNull()
    // A changed field or a newly added value is still checked.
    expect(validateMetadata({ ...fields, title: 'New\u0001' }, attrs, t, stored)).toContain('validationCtrlChar')
    expect(validateMetadata(fields, { 'bad,key': ['v\u0002', 'w\u0003'] }, t, stored))
      .toContain('validationAttrValCtrlChar')
    expect(validateMetadata(fields, { ...attrs, 'new:key': ['ok'] }, t, stored))
      .toContain('validationAttrKeyInvalidChars')
  })
})

describe('ACCEPTED_WSI_EXTENSIONS', () => {
  it('lists single-file formats only', () => {
    for (const ext of ['.mrxs', '.vms', '.vmu']) {
      expect(ACCEPTED_WSI_EXTENSIONS).not.toContain(ext)
    }
    expect(ACCEPTED_WSI_EXTENSIONS).toContain('.svs')
  })
})

describe('formatBytes', () => {
  it('uses binary units: whole B / KB, one decimal MB, two from GB', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1023)).toBe('1023 B')
    expect(formatBytes(1024)).toBe('1 KB')
    expect(formatBytes(1536)).toBe('2 KB')
    expect(formatBytes(3 * 1024 ** 2)).toBe('3.0 MB')
    expect(formatBytes(15.44 * 1024 ** 2)).toBe('15.4 MB')
    expect(formatBytes(1.5 * 1024 ** 3)).toBe('1.50 GB')
    expect(formatBytes(2 * 1024 ** 4)).toBe('2.00 TB')
    expect(formatBytes(3000 * 1024 ** 4)).toBe('3000.00 TB')
  })

  it('uses SI units with `decimal`', () => {
    expect(formatBytes(999, { decimal: true })).toBe('999 B')
    expect(formatBytes(1000, { decimal: true })).toBe('1 kB')
    expect(formatBytes(2_500_000, { decimal: true })).toBe('2.5 MB')
    expect(formatBytes(3_210_000_000, { decimal: true })).toBe('3.21 GB')
    expect(formatBytes(4e12, { decimal: true })).toBe('4.00 TB')
  })

  it('shows an unknown size as a dash', () => {
    expect(formatBytes(null)).toBe('—')
    expect(formatBytes(undefined)).toBe('—')
    expect(formatBytes(0 || null)).toBe('—')
  })
})

describe('displayFilename', () => {
  it('strips the upload-time id prefix', () => {
    expect(displayFilename('12_liver.svs')).toBe('liver.svs')
    expect(displayFilename('liver.svs')).toBe('liver.svs')
    expect(displayFilename('12_34_x.svs')).toBe('34_x.svs')
  })

  it("strips only the slide's own id prefix when given the id", () => {
    expect(displayFilename('12_liver.svs', 12)).toBe('liver.svs')
    expect(displayFilename('2024_scan.svs', 12)).toBe('2024_scan.svs')
    expect(displayFilename('slide-12', 12)).toBe('slide-12')
  })
})

describe('formatByteLimit', () => {
  it('names a size limit without trailing zeros', () => {
    expect(formatByteLimit(8 * 1024 ** 3)).toBe('8 GB')
    expect(formatByteLimit(Math.floor(2.5 * 1024 ** 2))).toBe('2.5 MB')
    expect(formatByteLimit(10_000_000_000)).toBe('9.31 GB')
    expect(formatByteLimit(512)).toBe('512 B')
  })
})

describe('exceedsUploadLimit', () => {
  it('refuses only above a known limit', () => {
    expect(exceedsUploadLimit(11, 10)).toBe(true)
    expect(exceedsUploadLimit(10, 10)).toBe(false)
    expect(exceedsUploadLimit(10 ** 15, null)).toBe(false)
    expect(exceedsUploadLimit(10 ** 15, undefined)).toBe(false)
  })
})
