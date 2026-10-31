// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { adminAssetPath } from './adminAssetPath'

describe('adminAssetPath', () => {
  it('accepts a tour attachment and returns the path to request', () => {
    expect(adminAssetPath('/assets/tours/abc123/figure.png')).toBe('/assets/tours/abc123/figure.png')
    expect(adminAssetPath('/assets/tours/abc123/a%20b.png?x=1')).toBe('/assets/tours/abc123/a%20b.png?x=1')
  })

  it.each([
    // The browser resolves dot segments (also when written %2e%2e or with
    // backslashes) before the request leaves, so each of these is requested as
    // something other than a tour attachment - and must never get the bearer.
    ['/assets/../api/admin/slides/12/download'],
    ['/assets/tours/../../api/admin/backup/x/download'],
    ['/assets/tours/abc/%2e%2e/%2e%2e/api/admin/slides/1/download'],
    ['/assets/./../api/admin/users'],
    ['/assets\\..\\api\\admin\\users'],
    // Not under /assets/tours/ at all.
    ['/assets/other/x.png'],
    ['/assets/x.png'],
    ['/api/admin/slides/12/download'],
    // Another origin, or a protocol-relative URL.
    ['//evil.example/assets/tours/a/b.png'],
    ['https://evil.example/assets/tours/a/b.png'],
    [''],
  ])('refuses %s', (src) => {
    expect(adminAssetPath(src)).toBeNull()
  })

  it('keeps an encoded slash literal, so what is requested is still under /assets/tours/', () => {
    // "%2f" is not a dot-segment separator to the URL parser; nginx refuses the
    // encoded separator outright, and the static mount re-checks containment.
    const out = adminAssetPath('/assets/tours/abc/..%2f..%2fapi')
    expect(out).toBe('/assets/tours/abc/..%2f..%2fapi')
    expect(new URL(out!, window.location.origin).pathname.startsWith('/assets/tours/')).toBe(true)
  })
})
