// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { PIN_COLORS, resolvePinColor } from './tourPins'

describe('resolvePinColor', () => {
  it.each(['#abc', '#abcd', '#aabbcc', '#aabbcc80'])('passes the valid CSS hex %s through', (c) => {
    expect(resolvePinColor(c)).toBe(c)
  })

  // 5- and 7-digit values are not CSS colors: the browser would drop the fill.
  it.each(['#abcde', '#aabbccd', '#ab', '#aabbccdd0', 'red"><x>'])('falls back to the palette for %s', (c) => {
    expect(resolvePinColor(c)).toBe(PIN_COLORS.red)
  })
})
