import { describe, expect, it } from 'vitest'
import { attributeParts, mergeAttributes } from './attributes'

describe('mergeAttributes', () => {
  it("unions per key, the slide's values first, each once", () => {
    expect(mergeAttributes(
      { stain: ['HE'], organ: ['Liver'] },
      { stain: ['PAS', 'HE'], level: ['basic'] },
    )).toEqual({ stain: ['HE', 'PAS'], organ: ['Liver'], level: ['basic'] })
  })

  it('takes either side missing', () => {
    expect(mergeAttributes(undefined, { level: ['basic'] })).toEqual({ level: ['basic'] })
    expect(mergeAttributes({ stain: ['HE'] }, null)).toEqual({ stain: ['HE'] })
  })
})

describe('attributeParts', () => {
  it('lists the keys in the given order, skipping empty ones', () => {
    expect(attributeParts({ stain: ['HE', 'PAS'], level: ['basic'], organ: [] }, ['level', 'organ', 'stain']))
      .toEqual(['basic', 'HE, PAS'])
  })
})
