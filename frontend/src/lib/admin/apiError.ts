import { isApiCodeError, isFileTooLargeError, isGatewayError, isNetworkError, isRateLimitError, isUnauthorizedError } from '../api/shared'
import type { TFn } from './validation'

// Field identifiers the backend sends under a `field` param (it can't send a
// translated label). Mapped to the same i18n keys the slide editor uses for
// those field headings so server-side and client-side validation read
// identically. Unknown fields fall through to the raw identifier.
const FIELD_LABEL_KEYS: Record<string, string> = {
  catalog_id: 'adminDash.fieldCatalogId',
  title: 'adminDash.fieldTitle',
  mpp_x: 'adminDash.fieldMppX',
  mpp_y: 'adminDash.fieldMppY',
  credit_text: 'adminDash.fieldCreditText',
  source_url: 'adminDash.fieldSourceUrl',
}

// Translate an error thrown by the API layer into a localized, human-readable
// string. ApiCodeError carries a `{code, params}` payload from the backend
// (see services/app/wsi_app/admin_helpers.py → api_error); everything else
// falls through to the default message handling, so endpoints that don't
// emit `{code, params}` are unaffected.
//
// `field` params are resolved to a localized `label` here because the message
// templates that consume them (e.g. validationMaxLen) expect an already
// localized label, and the backend only knows the field identifier.
export function apiErrorMessage(err: unknown, t: TFn): string {
  if (isApiCodeError(err)) {
    const params: Record<string, string | number> = { ...(err.params || {}) }
    const field = params.field
    if (typeof field === 'string') {
      const key = FIELD_LABEL_KEYS[field]
      params.label = key ? t(key) : field
      delete params.field
    }
    return t(err.code, params)
  }
  if (isRateLimitError(err)) return t('common.rateLimited')
  // Callers that know the applicable cap say so themselves (common.fileTooLarge
  // with a limit); here the limit is the server's and not known to the UI.
  if (isFileTooLargeError(err)) return t('common.fileTooLargeServer')
  // Its message is a bare "unauthorized-401" sentinel, not meant for display.
  if (isUnauthorizedError(err)) return t('common.sessionExpired')
  // Bare "network-error" sentinel, likewise not meant for display.
  if (isNetworkError(err)) return t('common.networkError')
  // Bare "gateway-error-<status>" sentinel (a proxy page, not an app answer).
  if (isGatewayError(err)) return t('common.gatewayError', { status: err.status })
  return (err as Error)?.message ?? ''
}

// "<headline>: <detail>", or the headline alone when there is no detail to add
// (an error with an empty message, a per-item failure without a reason).
export function withErrorDetail(headline: string, detail: string | null | undefined): string {
  return detail ? `${headline}: ${detail}` : headline
}

// A failure headline ("Failed to delete slide") followed by the localized
// reason apiErrorMessage finds in `err`, if any.
export function failureMessage(headline: string, err: unknown, t: TFn): string {
  return withErrorDetail(headline, apiErrorMessage(err, t))
}

// apiErrorMessage for the errors that carry their own localized message (a
// server `{code, params}`, 429, 413, 401, transport failure); `fallbackKey`'s
// generic text for the rest, whose raw message is English server / fetch text.
export function apiErrorMessageOr(err: unknown, t: TFn, fallbackKey: string): string {
  return hasLocalizedMessage(err) ? apiErrorMessage(err, t) : t(fallbackKey)
}

// apiErrorMessage when it is in the user's language, '' for raw English
// server / fetch text: the reason to append to a localized headline.
export function localizedErrorDetail(err: unknown, t: TFn): string {
  return hasLocalizedMessage(err) ? apiErrorMessage(err, t) : ''
}

function hasLocalizedMessage(err: unknown): boolean {
  return isApiCodeError(err) || isRateLimitError(err) || isFileTooLargeError(err)
    || isUnauthorizedError(err) || isNetworkError(err) || isGatewayError(err)
}
