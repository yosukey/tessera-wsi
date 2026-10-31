// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fakeFetch } from '../../test/fakeFetch'
import { apiErrorMessage } from '../admin/apiError'
import en from '../i18n/en'
import { clearToken, setToken } from './auth'
import { ApiCodeError, UnauthorizedError } from './shared'
import { createSyncSession, endSyncSession } from './sync'

const t = ((key: string) => (en as Record<string, unknown>)[key] as string) as never

beforeEach(() => setToken('tok'))
afterEach(() => {
  clearToken()
  vi.unstubAllGlobals()
})

describe('createSyncSession errors are localizable', () => {
  it('without a token it throws UnauthorizedError and sends nothing', async () => {
    clearToken()
    const { fn } = fakeFetch([])
    await expect(createSyncSession(1)).rejects.toBeInstanceOf(UnauthorizedError)
    expect(fn).not.toHaveBeenCalled()
  })

  it('a rejected token throws UnauthorizedError (shown as "session expired")', async () => {
    fakeFetch([{ status: 401, body: { detail: 'Invalid token' } }])
    const err = await createSyncSession(1).catch((e) => e)
    expect(err).toBeInstanceOf(UnauthorizedError)
    expect(apiErrorMessage(err, t)).toBe(en['common.sessionExpired'])
  })

  it.each([
    [404, 'sync.apiErr.slideNotFound'],
    [403, 'sync.apiErr.slideUnpublished'],
    [503, 'sync.apiErr.tooManySessions'],
  ])('a %i refusal carries the code %s', async (status, code) => {
    fakeFetch([{ status, body: { detail: { code } } }])
    const err = await createSyncSession(1).catch((e) => e)
    expect(err).toBeInstanceOf(ApiCodeError)
    expect(apiErrorMessage(err, t)).toBe((en as Record<string, unknown>)[code])
  })
})

describe('endSyncSession scope', () => {
  it('the unmount end is scoped by the presenter client id', async () => {
    const { requests } = fakeFetch([{ body: { ok: true } }])
    await endSyncSession('S 1', 'p-abc')
    expect(requests[0]).toMatchObject({
      url: '/api/admin/sync/sessions/S%201?client_id=p-abc',
      method: 'DELETE',
    })
  })

  it('an explicit end carries no scope', async () => {
    const { requests } = fakeFetch([{ body: { ok: true } }])
    await endSyncSession('S1')
    expect(requests[0].url).toBe('/api/admin/sync/sessions/S1')
  })
})

describe('ending without a token and peek error classification', () => {
  it('ending without a token says so instead of resolving quietly', async () => {
    clearToken()
    const { fn } = fakeFetch([])
    await expect(endSyncSession('S1')).rejects.toBeInstanceOf(UnauthorizedError)
    expect(fn).not.toHaveBeenCalled()
  })

  it.each([429, 500, 502, 503])('a %i peek is reported as retryable', async (status) => {
    const { peekSyncSession, isSyncPeekUnavailableError } = await import('./sync')
    fakeFetch([{ status, body: { detail: 'x' } }])
    const err = await peekSyncSession('S1').catch((e) => e)
    expect(isSyncPeekUnavailableError(err)).toBe(true)
  })

  it('a peek with no response at all is retryable too; a 404 is no session', async () => {
    const { peekSyncSession, isSyncPeekUnavailableError } = await import('./sync')
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))
    expect(isSyncPeekUnavailableError(await peekSyncSession('S1').catch((e) => e))).toBe(true)
    fakeFetch([{ status: 404, body: { detail: 'Session not found' } }])
    expect(await peekSyncSession('S1')).toBeNull()
  })

  it('a 403 peek is final', async () => {
    const { peekSyncSession, isSyncPeekUnavailableError } = await import('./sync')
    fakeFetch([{ status: 403, body: { detail: 'Forbidden' } }])
    const err = await peekSyncSession('S1').catch((e) => e)
    expect(err).toBeInstanceOf(Error)
    expect(isSyncPeekUnavailableError(err)).toBe(false)
  })
})
