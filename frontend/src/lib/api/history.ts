// Admin audit-history API client (GET /api/admin/history).
import { adminGet, authHeaders, buildQuery, downloadCsv } from './shared'
import type { ServerTz } from './analytics'

export type AuditLogItem = {
  id: number
  occurred_at: string
  actor_id: number | null
  actor_username: string
  actor_role: string | null
  action: string
  target: string | null
  details: Record<string, unknown>
}

export type AuditLogResponse = {
  total: number
  // The newest id the pages are read from: pass it back as `max_id` so events
  // recorded while paging don't shift later pages.
  snapshot_id: number
  // Matching events recorded after the snapshot (offer a reload).
  newer: number
  // Zone the since/until filters are read in; times are rendered in it too.
  server_tz: ServerTz
  items: AuditLogItem[]
}

export type AuditLogQuery = {
  limit?: number
  offset?: number
  // Exact action name, or a category prefix ending in "." (e.g. "slide.").
  action?: string
  // Part of the user name (for a failed login: of the name tried).
  actor?: string
  // Part of the target.
  target?: string
  // Only events with no user ("System").
  system?: boolean
  // YYYY-MM-DD, inclusive. The server reads the day in the analytics timezone
  // (WSI_ANALYTICS_TIMEZONE / host TZ), not UTC and not the browser's zone.
  since?: string
  until?: string
  max_id?: number
}

export async function adminGetAuditLogs(
  params: AuditLogQuery = {},
  signal?: AbortSignal,
): Promise<AuditLogResponse> {
  return adminGet<AuditLogResponse>(
    `/api/admin/history${buildQuery(params)}`,
    'history failed',
    { signal, cache: 'no-store' },
  )
}

// Stream the audit log to a CSV download, honouring the same filters as the
// on-screen table (but without the pagination limit/offset — the server caps
// the row count).
export function adminDownloadAuditLogsCsv(
  params: Omit<AuditLogQuery, 'limit' | 'offset' | 'max_id'> = {},
  opts: { signal?: AbortSignal; onProgress?: (receivedBytes: number) => void } = {},
): Promise<void> {
  const qs = buildQuery({
    action: params.action,
    actor: params.actor,
    target: params.target,
    system: params.system || undefined,
    since: params.since,
    until: params.until,
  })
  return downloadCsv(`/api/admin/history/export.csv${qs}`, {
    headers: authHeaders(),
    fallbackName: 'audit-history.csv',
    failMessage: 'history csv export failed',
    ...opts,
  })
}
