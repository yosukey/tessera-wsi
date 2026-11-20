import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf-8')
const viewport = /<meta\s+name="viewport"\s+content="([^"]*)"/.exec(html)?.[1] ?? ''
const keys = viewport.split(',').map((part) => part.trim())

describe('index.html viewport meta', () => {
  it('lays the page out at the device width, unscaled', () => {
    expect(keys).toContain('width=device-width')
    expect(keys).toContain('initial-scale=1.0')
  })

  // iPadOS Safari otherwise widens the layout and zooms the page out whenever
  // the content is laid out wider than the screen — which the first layout
  // after a rotation can be — and keeps it so until the next rotation.
  it('opts out of iPadOS shrink-to-fit', () => {
    expect(keys).toContain('shrink-to-fit=no')
  })
})
