import { describe, expect, it } from 'vitest'
import { dziDescriptorUrl, dziTileUrl, sziArchiveUrl, tileVersionQuery } from './tileUrl'

describe('tileVersionQuery', () => {
  it('emits ?v= only for a canonical non-negative integer', () => {
    expect(tileVersionQuery(1)).toBe('?v=1')
    expect(tileVersionQuery(0)).toBe('?v=0')
    // fresh_wsi_generation() (a re-imported slide) starts at epoch milliseconds.
    expect(tileVersionQuery(1759651234567)).toBe('?v=1759651234567')
    // Anything the server's canonical-decimal check would refuse is dropped,
    // leaving the unversioned (served, never edge-cached) URL.
    for (const bad of [undefined, null, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, 2 ** 60]) {
      expect(tileVersionQuery(bad)).toBe('')
    }
  })
})

describe('slide URLs', () => {
  it('encode the public_id and carry the generation', () => {
    expect(dziDescriptorUrl('a_B-9', 4)).toBe('/dzi/a_B-9.dzi?v=4')
    expect(sziArchiveUrl('a_B-9', 4)).toBe('/dzi/a_B-9.szi?v=4')
    expect(dziTileUrl('a_B-9', 10, 2, 3, 'jpeg', 4)).toBe('/dzi/a_B-9_files/10/2_3.jpeg?v=4')
    expect(dziDescriptorUrl('a b')).toBe('/dzi/a%20b.dzi')
  })
})
