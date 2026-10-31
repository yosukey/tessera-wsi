import { describe, expect, it } from 'vitest'
import { loginUrlFor, sanitizeNext } from './nextPath'

describe('sanitizeNext', () => {
  it('accepts same-origin relative paths, query and hash included', () => {
    for (const ok of ['/', '/slides/abc?sync=Xy_9', '/tours/t1#step-2', '/loginx', '/a%2F%2Fb']) {
      expect(sanitizeNext(ok)).toBe(ok)
    }
  })

  it('rejects anything that could leave the origin or loop', () => {
    const bad = [
      null, undefined, '', 'slides/x', '//evil.com', '//evil.com/x', '/\\evil.com', '/a\\b',
      'https://evil.com/', 'javascript:alert(1)', '/ok\r\nx', '/ok\tx', '/login',
      '/login?next=/x', '/login/', '/' + 'a'.repeat(3000),
    ]
    for (const b of bad) expect(sanitizeNext(b as string | null | undefined)).toBeNull()
  })
})

describe('loginUrlFor', () => {
  it('carries the full target, encoded', () => {
    expect(loginUrlFor({ pathname: '/slides/abc', search: '?sync=tok&a=1', hash: '' }))
      .toBe('/login?next=' + encodeURIComponent('/slides/abc?sync=tok&a=1'))
  })

  it('omits next for the root path', () => {
    expect(loginUrlFor({ pathname: '/', search: '', hash: '' })).toBe('/login')
  })
})
