// Admin error-log API client (GET /api/admin/logs). Mirrors ./history.
import { adminFetch, adminGet, authHeaders, buildQuery, downloadCsv } from './shared'
import type { ServerTz } from './analytics'

export type ErrorLogItem = {
  id: number
  occurred_at: string
  level: string
  logger: string
  message: string
  module: string | null
  func: string | null
  line: number | null
  // The listing leaves the traceback out (up to 16 KB a row); it is fetched
  // on demand with adminGetErrorLog, which fills `traceback`.
  has_traceback: boolean
  traceback?: string
  request_path: string | null
  request_method: string | null
}

export type ErrorLogResponse = {
  total: number
  // The newest id the pages are read from: pass it back as `max_id` so rows
  // logged while paging don't shift later pages.
  snapshot_id: number
  // Matching rows logged after the snapshot (offer a reload).
  newer: number
  // Rows per level under every filter but the level.
  level_counts: Record<string, number>
  // Zone the since/until filters are read in; times are rendered in it too.
  server_tz: ServerTz
  items: ErrorLogItem[]
}

export type ErrorLogQuery = {
  limit?: number
  offset?: number
  level?: string // exact level name, e.g. "ERROR"
  logger?: string // exact logger name, or a prefix ending in "."
  search?: string // substring match on the message, traceback and request path
  // YYYY-MM-DD, inclusive. The server reads the day in the analytics timezone
  // (WSI_ANALYTICS_TIMEZONE / host TZ), not UTC and not the browser's zone.
  since?: string
  until?: string
  max_id?: number
}

export async function adminGetErrorLogs(
  params: ErrorLogQuery = {},
  signal?: AbortSignal,
): Promise<ErrorLogResponse> {
  return adminGet<ErrorLogResponse>(
    `/api/admin/logs${buildQuery(params)}`,
    'logs failed',
    { signal, cache: 'no-store' },
  )
}

// One row with its traceback.
export async function adminGetErrorLog(id: number, signal?: AbortSignal): Promise<ErrorLogItem> {
  return adminGet<ErrorLogItem>(`/api/admin/logs/${id}`, 'log failed', { signal, cache: 'no-store' })
}

// Delete the stored rows up to `maxId` (the listing's snapshot_id), so a row
// logged since the operator looked is never cleared unseen. journald keeps
// every line; the clear is audited.
export async function adminClearErrorLogs(maxId: number): Promise<{ deleted: number }> {
  const r = await adminFetch(`/api/admin/logs${buildQuery({ max_id: maxId })}`, 'logs clear failed', {
    method: 'DELETE',
    cache: 'no-store',
  })
  return r.json()
}

// Stream the error log to a CSV download, honouring the same filters as the
// on-screen table (but without the pagination limit/offset — the server caps
// the row count).
export function adminDownloadErrorLogsCsv(
  params: Omit<ErrorLogQuery, 'limit' | 'offset' | 'max_id'> & { include_traceback?: boolean } = {},
  opts: { signal?: AbortSignal; onProgress?: (receivedBytes: number) => void } = {},
): Promise<void> {
  const qs = buildQuery({
    level: params.level,
    logger: params.logger,
    search: params.search,
    since: params.since,
    until: params.until,
    include_traceback: params.include_traceback,
  })
  return downloadCsv(`/api/admin/logs/export.csv${qs}`, {
    headers: authHeaders(),
    fallbackName: 'error-logs.csv',
    failMessage: 'logs csv export failed',
    ...opts,
  })
}
