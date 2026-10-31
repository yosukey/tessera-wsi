// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  _resetLoginRedirectForTests,
  adminFetch,
  adminGet,
  adminSend,
  authHeaders,
  bearerHeaders,
  downloadAttachment,
  errorGuard,
  httpStatusOf,
  isNetworkError,
  isRateLimitError,
  isUnauthorizedError,
  listSlides,
  NetworkError,
  RateLimitError,
  throwApi,
  UnauthorizedError,
} from './shared'
import { STORAGE_KEYS } from '../storageKeys'

function jsonResponse(status: number, detail: string): Response {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

afterEach(() => {
  _resetLoginRedirectForTests()
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('throwApi 401 handling', () => {
  it('turns a rejected staff token into UnauthorizedError', async () => {
    const err = await throwApi(jsonResponse(401, 'Invalid token'), 'x').catch((e) => e)
    expect(isUnauthorizedError(err)).toBe(true)
  })

  it('keeps other 401s (wrong login or restore password) as plain errors', async () => {
    for (const detail of ['Invalid credentials', 'password incorrect']) {
      const err = await throwApi(jsonResponse(401, detail), 'x').catch((e) => e)
      expect(isUnauthorizedError(err)).toBe(false)
      expect(err.message).toBe(detail)
    }
  })

  it('clears the token and returns to /admin from an admin sub-page', async () => {
    const replace = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, pathname: '/admin/account', replace },
    })
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 'stale')
    await throwApi(jsonResponse(401, 'Invalid token'), 'x').catch(() => {})
    expect(replace).toHaveBeenCalledWith('/admin')
    expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBeNull()
  })

  it('does not redirect from the login page itself', async () => {
    const replace = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, pathname: '/admin', replace },
    })
    await throwApi(jsonResponse(401, 'Invalid token'), 'x').catch(() => {})
    expect(replace).not.toHaveBeenCalled()
  })

  it('redirects once when several requests fail with 401 together, and handles Missing token', async () => {
    const replace = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, pathname: '/admin/backup', replace },
    })
    await Promise.all([
      throwApi(jsonResponse(401, 'Invalid token'), 'x').catch(() => {}),
      throwApi(jsonResponse(401, 'Missing token'), 'x').catch(() => {}),
      throwApi(jsonResponse(401, 'Invalid token'), 'x').catch(() => {}),
    ])
    expect(replace).toHaveBeenCalledTimes(1)
  })

  it('tries again later when the first redirect was held back (beforeunload prompt declined)', async () => {
    vi.useFakeTimers()
    try {
      const replace = vi.fn()
      Object.defineProperty(window, 'location', {
        configurable: true,
        value: { ...window.location, pathname: '/admin/dashboard', replace },
      })
      await throwApi(jsonResponse(401, 'Invalid token'), 'x').catch(() => {})
      await throwApi(jsonResponse(401, 'Invalid token'), 'x').catch(() => {})
      expect(replace).toHaveBeenCalledTimes(1) // concurrent 401s: still once
      vi.advanceTimersByTime(3500) // the user stayed on the page
      await throwApi(jsonResponse(401, 'Invalid token'), 'x').catch(() => {})
      expect(replace).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('httpStatusOf', () => {
  it('reads the status of an uncoded failure, which stays a plain Error', async () => {
    const err = await throwApi(jsonResponse(403, 'Forbidden'), 'x').catch((e) => e)
    expect(httpStatusOf(err)).toBe(403)
    expect(err.message).toBe('Forbidden')
    expect(isUnauthorizedError(err)).toBe(false)
    expect(err).toEqual(new Error('Forbidden'))
    expect(httpStatusOf(await throwApi(jsonResponse(500, 'boom'), 'x').catch((e) => e))).toBe(500)
  })

  it('reads UnauthorizedError\'s status and nothing from other values', () => {
    expect(httpStatusOf(new UnauthorizedError(401))).toBe(401)
    for (const v of [new Error('x'), new NetworkError(), null, undefined, 403, { status: '403' }]) {
      expect(httpStatusOf(v)).toBeUndefined()
    }
  })
})

describe('errorGuard', () => {
  it('recognises an instance and a same-named error from another module copy', () => {
    expect(isRateLimitError(new RateLimitError())).toBe(true)
    const foreign = Object.assign(new Error('x'), { name: 'RateLimitError' })
    expect(isRateLimitError(foreign)).toBe(true)
  })

  it('rejects other errors and non-objects', () => {
    expect(isRateLimitError(new NetworkError())).toBe(false)
    expect(isNetworkError(new Error('network-error'))).toBe(false)
    for (const v of [null, undefined, 'RateLimitError', 429]) expect(isRateLimitError(v)).toBe(false)
  })

  it('covers subclasses, which keep the parent name', () => {
    class Sub extends UnauthorizedError {}
    expect(isUnauthorizedError(new Sub(403))).toBe(true)
  })

  it('builds a guard for a caller-defined class', () => {
    class GoneError extends Error {
      constructor() { super('gone'); this.name = 'GoneError' }
    }
    const isGone = errorGuard(GoneError, 'GoneError')
    expect(isGone(new GoneError())).toBe(true)
    expect(isGone(new Error('gone'))).toBe(false)
  })
})

describe('auth headers', () => {
  it('carries the stored staff token, merged with extra headers', () => {
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 'tok')
    expect(authHeaders()).toEqual({ Authorization: 'Bearer tok' })
    expect(authHeaders({ 'Content-Type': 'application/json' }))
      .toEqual({ Authorization: 'Bearer tok', 'Content-Type': 'application/json' })
  })

  it('never sends "Bearer null": no token, no Authorization header', () => {
    expect(authHeaders()).toEqual({})
    expect(authHeaders({ 'Content-Type': 'application/json' })).toEqual({ 'Content-Type': 'application/json' })
    expect(bearerHeaders(null)).toEqual({})
    expect(bearerHeaders(undefined)).toEqual({})
    expect(bearerHeaders('')).toEqual({})
    expect(bearerHeaders('x')).toEqual({ Authorization: 'Bearer x' })
  })
})

describe('admin request helpers', () => {
  function stubFetch(response: () => Response) {
    const fn = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => response())
    vi.stubGlobal('fetch', fn)
    return fn
  }
  const ok = (body: unknown) => () => new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })

  afterEach(() => vi.unstubAllGlobals())

  it('adminGet sends the token, signal and cache mode and answers the JSON body', async () => {
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 'tok')
    const fn = stubFetch(ok({ a: 1 }))
    const ctrl = new AbortController()
    await expect(adminGet('/api/x', 'x failed', { signal: ctrl.signal, cache: 'no-store' })).resolves.toEqual({ a: 1 })
    const init = fn.mock.calls[0][1]!
    expect(init.headers).toEqual({ Authorization: 'Bearer tok' })
    expect(init.signal).toBe(ctrl.signal)
    expect(init.cache).toBe('no-store')
    expect(init.method).toBeUndefined()
  })

  it('adminSend encodes a plain object as JSON', async () => {
    const fn = stubFetch(ok({ ok: true }))
    await adminSend('PATCH', '/api/x', { a: [1] }, 'x failed')
    const init = fn.mock.calls[0][1]!
    expect(init.method).toBe('PATCH')
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(init.body).toBe('{"a":[1]}')
  })

  it('adminSend passes FormData through untouched, without a Content-Type', async () => {
    const fn = stubFetch(ok({ ok: true }))
    const form = new FormData()
    form.append('k', 'v')
    await adminSend('POST', '/api/x', form, 'x failed')
    const init = fn.mock.calls[0][1]!
    expect(init.body).toBe(form)
    expect(init.headers).toEqual({})
  })

  it('adminSend without a body sends none', async () => {
    const fn = stubFetch(ok({ ok: true }))
    await adminSend('DELETE', '/api/x', undefined, 'x failed')
    const init = fn.mock.calls[0][1]!
    expect(init.method).toBe('DELETE')
    expect(init.body).toBeUndefined()
  })

  it('adminFetch turns a failed answer into the typed error throwApi derives', async () => {
    stubFetch(() => new Response('<html>429</html>', { status: 429, headers: { 'content-type': 'text/html' } }))
    expect(isRateLimitError(await adminFetch('/api/x', 'x failed').catch((e) => e))).toBe(true)
    stubFetch(() => new Response('', { status: 500 }))
    expect((await adminFetch('/api/x', 'x failed').catch((e) => e)).message).toBe('x failed')
  })
})

describe('listSlides', () => {
  it('bypasses the browser cache, so a cached stale redirect is never replayed', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response(JSON.stringify({ items: [], total: 0, page: 1, per_page: 1 })),
    )
    await listSlides(new URLSearchParams({ page: '1', per_page: '2000', sort: 'catalog_id' }))
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/slides?page=1&per_page=2000&sort=catalog_id',
      expect.objectContaining({ cache: 'no-store' }),
    )
  })
})

describe('downloadAttachment', () => {
  function streamed(chunks: string[]): Response {
    const enc = new TextEncoder()
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        for (const ch of chunks) c.enqueue(enc.encode(ch))
        c.close()
      },
    })
    return new Response(body, {
      status: 200,
      headers: { 'content-type': 'text/csv', 'content-disposition': 'attachment; filename="x.csv"' },
    })
  }

  it('reports the bytes received and saves the whole body', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => streamed(['abc', 'defgh'])))
    const created: Blob[] = []
    vi.spyOn(URL, 'createObjectURL').mockImplementation((b: Blob | MediaSource) => { created.push(b as Blob); return 'blob:x' })
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const progress: number[] = []
    await downloadAttachment('/x', { fallbackName: 'f.csv', failMessage: 'x', onProgress: n => progress.push(n) })
    expect(progress).toEqual([3, 8])
    expect(await created[0].text()).toBe('abcdefgh')
    expect(click).toHaveBeenCalledTimes(1)
    vi.unstubAllGlobals()
  })

  it('passes the abort signal to the request', async () => {
    const fetchMock = vi.fn(async (_u: string, init?: RequestInit) => {
      expect(init?.signal?.aborted).toBe(true)
      throw new DOMException('aborted', 'AbortError')
    })
    vi.stubGlobal('fetch', fetchMock)
    const ctrl = new AbortController()
    ctrl.abort()
    await expect(downloadAttachment('/x', { fallbackName: 'f', failMessage: 'x', signal: ctrl.signal }))
      .rejects.toThrow(/aborted/)
    vi.unstubAllGlobals()
  })
})
