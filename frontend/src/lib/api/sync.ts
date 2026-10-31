import {
  NetworkError,
  RateLimitError,
  UnauthorizedError,
  bearerHeaders,
  errorGuard,
  getToken,
  isGatewayStatus,
  throwApi,
} from './shared'

export type SyncSessionInfo = {
  session_id: string
  slide_id: number
  // The slide's public_id: what the follower's browser navigates to (viewer
  // URLs are keyed by the token, not the numeric id).
  slide_public_id: string
  slide_title: string | null
  slide_catalog_id: string | null
  // Mirrors WSI_SYNC_REQUIRE_NAME on the server. The join dialog hides
  // its "Join anonymously" button when true; without this signal a
  // follower could submit an anonymous hello and get stuck in a
  // reconnect loop on the server's 4400 "name required" close.
  require_name: boolean
  // Present only on the owner-scoped list (listOwnedSyncSessions), which
  // backs the presenter's "resume" affordance. The server builds it with the
  // same canonicalisation as create (honouring WSI_PUBLIC_BASE_URL), so resume
  // can reuse it verbatim instead of rebuilding the link from window.location
  // — which would pin the share URL to whichever host the presenter is on.
  // Null on the public peek.
  follower_url?: string | null
  // Owner-scoped list only: whether a presenter socket is attached right now
  // (another tab/device, or this browser's previous socket the server has not
  // yet seen die). A Resume takes the session over either way, so the session
  // is still offered for resuming rather than hidden behind Start — which
  // would supersede it and end it for every follower.
  presenter_connected?: boolean | null
}

export type CreatedSyncSession = {
  session_id: string
  slide_id: number
  slide_public_id: string
  follower_url: string
}

// One active live-sync session as seen by the staff management view
// (adminListAllSyncSessions). Carries the triage fields needed to find and
// force-end an orphan: who started it, whether a presenter socket is currently
// attached, the live follower count, when it started, and — for a
// presenter-less room — the seconds left before the server's grace GC reaps it.
export type AdminSyncSessionInfo = {
  session_id: string
  slide_id: number
  slide_title: string | null
  slide_catalog_id: string | null
  owner_user_id: number
  owner_username: string | null
  owner_role: string | null
  presenter_connected: boolean
  follower_count: number
  // Epoch seconds (server wall clock).
  created_at: number
  // Seconds until the grace GC reaps a presenter-less room; null while a
  // presenter is attached (those are bounded only by the absolute age cap).
  grace_remaining: number | null
}

// A missing or rejected staff token throws UnauthorizedError (no round trip
// without a token): the caller maps it to sync.loginRequired /
// common.sessionExpired. The server's own refusals (unknown or unpublished
// slide, too many sessions) arrive as coded ApiCodeErrors.
export async function createSyncSession(slideId: number): Promise<CreatedSyncSession> {
  const token = getToken()
  if (!token) throw new UnauthorizedError(401)
  const r = await fetch('/api/admin/sync/sessions', {
    method: 'POST',
    headers: bearerHeaders(token, { 'Content-Type': 'application/json' }),
    body: JSON.stringify({ slide_id: slideId }),
  })
  if (r.status === 401) throw new UnauthorizedError(401)
  if (!r.ok) await throwApi(r, 'create sync session failed')
  return r.json()
}

export async function endSyncSession(sid: string, clientId?: string | null): Promise<void> {
  const token = getToken()
  // Without a token the DELETE could only 401, so none is sent — but the
  // caller is told: resolving quietly would let the admin session list drop the row
  // as if the session had ended. The presenter's own paths swallow it (a room
  // whose presenter left is reaped by the grace GC anyway).
  if (!token) throw new UnauthorizedError(401)
  // When the presenter's client id is supplied (the presenter-unmount "end
  // because I'm leaving" path), scope the teardown to it: the server ignores
  // the DELETE if another tab/device has attached since. Explicit ends (Stop
  // button, admin session management) pass no id and end unconditionally.
  const query = clientId ? `?client_id=${encodeURIComponent(clientId)}` : ''
  const r = await fetch(`/api/admin/sync/sessions/${encodeURIComponent(sid)}${query}`, {
    method: 'DELETE',
    headers: bearerHeaders(token),
    keepalive: true,
  })
  if (!r.ok && r.status !== 404) await throwApi(r, 'end sync session failed')
}

// A peek the server could not answer right now: rate limited (a class behind
// one NAT address opening the shared link together), a 5xx / gateway error
// (the single-worker sync service restarting) or no response at all. The
// follower retries these on its own; any other failure is final.
export class SyncPeekUnavailableError extends Error {
  // The underlying RateLimitError / NetworkError, when there is one.
  readonly reason?: Error
  constructor(reason?: Error) {
    super('sync-peek-unavailable')
    this.name = 'SyncPeekUnavailableError'
    this.reason = reason
  }
}

export const isSyncPeekUnavailableError = errorGuard(SyncPeekUnavailableError, 'SyncPeekUnavailableError')

export async function peekSyncSession(sid: string): Promise<SyncSessionInfo | null> {
  let r: Response
  try {
    r = await fetch(`/api/sync/sessions/${encodeURIComponent(sid)}`)
  } catch {
    throw new SyncPeekUnavailableError(new NetworkError())
  }
  if (r.status === 404) return null
  if (r.status === 429) throw new SyncPeekUnavailableError(new RateLimitError())
  if (r.status >= 500 || isGatewayStatus(r.status)) throw new SyncPeekUnavailableError()
  if (!r.ok) await throwApi(r, 'peek sync session failed')
  return r.json()
}

// Every active live-sync session, for the staff session-management surface
// (AdminServerPage). Backs the operator's force-end affordance for orphans
// that can no longer be reached from a presenter tab. Staff-only. A missing /
// expired token throws UnauthorizedError instead of resolving to [] — an
// empty list would read as "no live sessions" to the operator.
export async function adminListAllSyncSessions(): Promise<AdminSyncSessionInfo[]> {
  const token = getToken()
  if (!token) throw new UnauthorizedError(401)
  const r = await fetch('/api/admin/sync/sessions/all', {
    headers: bearerHeaders(token),
  })
  if (r.status === 401 || r.status === 403) throw new UnauthorizedError(r.status)
  if (!r.ok) await throwApi(r, 'list all sync sessions failed')
  const body = await r.json()
  return body.sessions ?? []
}

// Live sessions owned by the current admin, used by the viewer page to
// offer a "resume" affordance after the presenter left a still-live session.
// Staff-only (the server scopes the result to the caller); returns [] when no
// token is present rather than making a request bound to be refused.
export async function listOwnedSyncSessions(): Promise<SyncSessionInfo[]> {
  const token = getToken()
  if (!token) return []
  const r = await fetch('/api/admin/sync/sessions', {
    headers: bearerHeaders(token),
  })
  if (r.status === 401) return []
  if (!r.ok) await throwApi(r, 'list owned sync sessions failed')
  const body = await r.json()
  return body.sessions ?? []
}
