// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { sanitizeName } from './FollowerJoinDialog'

// The same rule as the server (sync_room.sanitize_name, test_sync_room.py).
describe('the join dialog names a follower as the server will', () => {
  it('keeps a wide space and joined emoji, drops bidi overrides', () => {
    expect(sanitizeName('山田\u3000太郎')).toBe('山田\u3000太郎')
    const family = '\u{1F468}\u200d\u{1F469}\u200d\u{1F467}'
    expect(sanitizeName(family)).toBe(family)
    expect(sanitizeName('abc\u202edef')).toBe('abcdef')
    expect(sanitizeName('a\u2028b')).toBe('a b')
    expect(sanitizeName('  Yamada\u0000 Taro \n')).toBe('Yamada Taro')
  })

  it('treats a name of invisible characters only as no name', () => {
    expect(sanitizeName('\u200b\u200b')).toBe('')
    expect(sanitizeName('\u3000\u200d')).toBe('')
  })

  it('caps at 64 code points', () => {
    expect(Array.from(sanitizeName('\u{1F600}'.repeat(100)))).toHaveLength(64)
  })

  it('cuts before judging visibility, as the server does', () => {
    expect(sanitizeName('\u200d'.repeat(64) + 'a')).toBe('')
  })

  it('keeps code points one side may not know yet', () => {
    expect(sanitizeName('\u{1FAE9}')).toBe('\u{1FAE9}')
    expect(sanitizeName('\ue000x')).toBe('x')
  })
})
