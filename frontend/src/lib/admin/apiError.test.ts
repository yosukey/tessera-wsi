import { describe, expect, it } from 'vitest'
import { apiErrorMessage, failureMessage, withErrorDetail } from './apiError'
import { ApiCodeError, FileTooLargeError, NetworkError, RateLimitError, UnauthorizedError } from '../api/shared'

// Echo the key and params so the assertions see which message was picked.
const t = (key: string, params?: Record<string, string | number>) =>
  params ? `${key}:${JSON.stringify(params)}` : key

describe('apiErrorMessage', () => {
  it('maps a 429 to the localized rate-limit text, not the sentinel', () => {
    expect(apiErrorMessage(new RateLimitError(), t)).toBe('common.rateLimited')
  })

  it('maps a 413 to the localized server-limit text', () => {
    expect(apiErrorMessage(new FileTooLargeError(), t)).toBe('common.fileTooLargeServer')
  })

  it('maps 401/403 to the session-expired text', () => {
    expect(apiErrorMessage(new UnauthorizedError(401), t)).toBe('common.sessionExpired')
  })

  it('maps a transport failure to the localized network text', () => {
    expect(apiErrorMessage(new NetworkError(), t)).toBe('common.networkError')
  })

  it('translates a coded error and resolves its field param to a label', () => {
    const err = new ApiCodeError('adminDash.apiErr.tooLong', { field: 'title', max: 10 })
    expect(apiErrorMessage(err, t)).toBe('adminDash.apiErr.tooLong:{"max":10,"label":"adminDash.fieldTitle"}')
  })

  it('falls back to the plain message of an uncoded error', () => {
    expect(apiErrorMessage(new Error('Duplicate filename'), t)).toBe('Duplicate filename')
  })

  it('returns an empty string for a non-error value', () => {
    expect(apiErrorMessage(undefined, t)).toBe('')
  })
})

describe('failureMessage / withErrorDetail', () => {
  it('appends the localized reason to the headline', () => {
    expect(failureMessage('Failed to delete slide', new RateLimitError(), t)).toBe('Failed to delete slide: common.rateLimited')
    expect(failureMessage('Failed', new Error('Duplicate filename'), t)).toBe('Failed: Duplicate filename')
  })

  it('shows the headline alone when there is no reason', () => {
    expect(failureMessage('Failed to delete slide', new Error(''), t)).toBe('Failed to delete slide')
    expect(failureMessage('Failed to delete slide', undefined, t)).toBe('Failed to delete slide')
    expect(withErrorDetail('Failed', null)).toBe('Failed')
    expect(withErrorDetail('Failed', 'locked')).toBe('Failed: locked')
  })
})
