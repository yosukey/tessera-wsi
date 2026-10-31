import {
  adminFetch,
  adminGet,
  adminSend,
  ApiCodeError,
  authHeaders,
  errorGuard,
  isGatewayStatus,
  NetworkError,
  throwApi,
  UnauthorizedError,
  xhrUpload,
} from './shared'
import { triggerDownload } from '../clipboard'

export type BackupScope = 'metadata' | 'full'

export type BackupItem = {
  id: string
  size_bytes: number
  created_at: string
  // '?' marks a row whose sidecar could not be read.
  scope: BackupScope | '?'
  app_version: string
  sha256: string
  note: string
  archive_format: string
  // Whether the ancillary DB table groups were captured. WSI presence is
  // conveyed by `scope` (full vs metadata). Every archive states these; the
  // listing endpoint falls back to true only for a sidecar too damaged to
  // read, so one bad row does not fail the whole list.
  includes_analytics: boolean
  includes_history: boolean
}

export type BackupListResponse = {
  current_app_version: string
  backup_dir: string
  items: BackupItem[]
}

// The three selectable components plus the always-included DB core, sized in
// (uncompressed) bytes. Drives the size hints next to the create-backup
// toggles. The real archive is smaller after compression.
export type BackupEstimate = {
  db_core: number
  analytics: number
  history: number
  wsi: number
  thumbs: number
}

export type BackupJob = {
  id: string
  // restore_apply covers a reset too. The slide transfer kinds come only from
  // adminGetActiveJob (the slot is shared with slide export / import).
  kind: 'backup' | 'backup_verify' | 'restore_stage' | 'restore_apply'
    | 'slide_export' | 'slide_import_stage' | 'slide_import' | 'slide_import_assets'
  // 'cancelled': a backup stopped by adminCancelBackupJob (no archive left).
  // Only backup jobs can be cancelled.
  state: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  created_at: string
  // Present while the job is pending / running: an operator has asked it to
  // stop (from any tab or worker) and it has not stopped yet.
  cancel_requested?: boolean
  progress: {
    phase?: string
    bytes_written?: number      // backup
    // Backup, phase "writing_wsi": the slide bytes read so far against the
    // snapshot's total (bytes_written is the compressed archive, whose final
    // size is not known up front).
    wsi_bytes_read?: number
    wsi_bytes_total?: number
    // Restore staging of an upload: phase "copying" while the received upload
    // is copied into staging, then "extracting" (bytes_extracted).
    bytes_copied?: number
    total_bytes?: number
    bytes_extracted?: number    // restore staging
    snapshot_id?: string | null // restore staging: the saved backup, or null for an upload
    stage_id?: string           // reset
    backup_id?: string          // backup_verify: the backup being checked
    members?: number            // backup_verify
    bytes_read?: number         // backup_verify (member content, uncompressed)
  }
  error: string | null
  // A failed job may carry a translatable `code` (with `params`), e.g.
  // adminDash.apiErr.uploadNoSpace for a staging volume that filled up.
  result: Record<string, any>
}

// The error a failed job stands for: its translatable code when it has one,
// else the server's (English) error text.
export function backupJobError(job: BackupJob, fallback: string): Error {
  const code = job.result?.code
  if (typeof code === 'string') return new ApiCodeError(code, job.result?.params ?? undefined)
  return new Error(job.error || fallback)
}

// Answer of the endpoints that start a background job.
export type JobStarted = { job_id: string }

export type RestorePlan = {
  backup_app_version: string
  current_app_version: string
  blocking_reason: string | null
  blocking_code: string | null
}

export type RestoreManifest = {
  scope: string
  app_version: string
  created_at: string
  slide_count: number
  tour_count: number
  note: string
  archive_format: string
}

// A restore staging job's result.
export type RestoreStageResponse = {
  stage_id: string
  manifest: RestoreManifest
  plan: RestorePlan
  current_app_version: string
}

export type RestoreApplyResponse = {
  ok: boolean
  mode: 'restart' | 'inprocess'
  manifest: RestoreManifest
  message?: string
}

export type RestoreResult = {
  result: null | {
    ok: boolean
    stage_id?: string
    scope?: string
    applied_at?: string
    app_version?: string
    error?: string
    rollback_root?: string
    // Number of slides referenced by the swapped-in DB whose wsi_filename
    // points at a file that no longer exists on disk. Populated by the
    // swap-on-boot script (and the dev in-process path on success);
    // surfaces operator-actionable "re-upload these WSIs" guidance for
    // metadata-scope restores and resets.
    wsi_uploads_pending?: number
    // The swap stands, but signing out the sessions issued before it failed:
    // tokens and cookies revoked since the backup may work again.
    session_revocation_error?: string
    // Dev in-process apply only: the swap stands (ok stays true), but
    // bringing the running app up to it failed afterwards — the same message
    // the apply answered with / its job failed with. post_apply_stage names
    // the step (schema / bootstrap / session_revocation). A restart finishes it.
    post_apply_error?: string
    post_apply_stage?: string
  }
}

export type IntegrityReport = {
  ok: boolean
  errors: string[]
  // Findings that do not block the restore (ok ignores them).
  warnings: string[]
  missing_wsi_files: string[]
  unexpected_wsi_files: string[]
}

export type LiveStateSummary = {
  slide_count: number
  tour_count: number
  wsi_file_count: number
  wsi_files_referenced_by_db: number
  wsi_total_bytes: number
  assets_total_bytes: number
}

export type ArchiveStateSummary = {
  scope: string
  slide_count: number
  tour_count: number
  wsi_file_count: number
  wsi_files_referenced_by_db: number
  wsi_total_bytes: number
  wsi_uploads_pending: number
}

export type RestorePreflightResponse = {
  stage_id: string
  scope: string
  manifest: RestoreManifest
  integrity: IntegrityReport
  live_state: LiveStateSummary
  archive_state: ArchiveStateSummary
}

export type ResetPreflightResponse = {
  live_state: LiveStateSummary
  preserved: { tables: string[] }
}

// The reset job's stage id comes with the job id: after the restart that
// applies it the job may be unreadable (the process is new, everyone is signed
// out), and the restore result names the stage it describes.
export type ResetStarted = JobStarted & {
  stage_id: string
  scope: 'reset'
}

// The server did not answer, or a gateway answered for it while the app was
// down or restarting (an HTML 502/503/504, or Cloudflare's 52x). The app's own
// 503 (a job that could not be recorded) is JSON and a definite refusal.
function isGatewayFailure(r: Response): boolean {
  return isGatewayStatus(r.status)
    && !(r.headers.get('content-type') || '').includes('application/json')
}

// The job's record is gone (pruned, or the job store was cleared): it will
// never report an outcome.
export class JobNotFoundError extends Error {
  constructor() {
    super('job-not-found')
    this.name = 'JobNotFoundError'
  }
}

export const isJobNotFoundError = errorGuard(JobNotFoundError, 'JobNotFoundError')

// A restore apply refused for the archive (409 { code, message }): the code
// (NO_VERSION / DOWNGRADE_NOT_SUPPORTED / SCHEMA_TOO_OLD /
// ARCHIVE_INCONSISTENT) has a
// localized text; the message is the server's English fallback.
export class RestoreRefusedError extends Error {
  code: string
  constructor(code: string, message: string) {
    super(message)
    this.name = 'RestoreRefusedError'
    this.code = code
  }
}

export const isRestoreRefusedError = errorGuard(RestoreRefusedError, 'RestoreRefusedError')

export type BackupComponents = {
  include_wsi: boolean
  include_analytics: boolean
  include_history: boolean
}

export async function adminCreateBackup(
  components: BackupComponents,
  note: string,
): Promise<{ job_id: string }> {
  return adminSend('POST', '/api/admin/backup', { ...components, note }, 'backup start failed')
}

export async function adminGetBackupEstimate(
  opts: { signal?: AbortSignal } = {},
): Promise<BackupEstimate> {
  return adminGet('/api/admin/backup/estimate', 'backup estimate failed', { signal: opts.signal, cache: 'no-store' })
}

// `keepLogin`: a 401 rejects with a plain UnauthorizedError instead of
// dropping the staff token and returning to the login screen - for following
// an apply, which signs everyone out (this tab included) as it lands. The
// caller then sends the operator to sign in with the apply's stage id, so the
// result is shown afterwards.
export async function adminGetBackupJob(
  jobId: string,
  opts: { signal?: AbortSignal; keepLogin?: boolean } = {},
): Promise<BackupJob> {
  const r = await fetch(`/api/admin/backup/jobs/${encodeURIComponent(jobId)}`, {
    headers: authHeaders(),
    signal: opts.signal,
    cache: 'no-store',
  })
  if (r.status === 404) throw new JobNotFoundError()
  if (r.status === 401 && opts.keepLogin) throw new UnauthorizedError(401)
  if (!r.ok) await throwApi(r, 'job poll failed')
  return r.json()
}

// The job holding the single backup / restore / import slot, whoever started
// it (another tab, another admin), or null when the slot is free. The id a tab
// keeps covers only jobs it started itself.
export async function adminGetActiveJob(opts: { signal?: AbortSignal } = {}): Promise<BackupJob | null> {
  const r = await adminGet<{ job: BackupJob | null }>(
    '/api/admin/backup/jobs/active',
    'active job failed',
    { signal: opts.signal, cache: 'no-store' },
  )
  return r.job
}

// Read a saved backup end to end (codec checksums and the sha256 recorded when
// it was made) without restoring it, as a job polled like any other: its
// result is { backup_id, sha256_checked, members, bytes }, and a damaged
// archive fails it with the reason.
export async function adminVerifyBackup(id: string): Promise<JobStarted> {
  return adminSend('POST', `/api/admin/backup/${encodeURIComponent(id)}/verify`, undefined, 'backup verify failed')
}

// A job state no further poll will change.
export function isTerminalJobState(state: string): boolean {
  return state === 'succeeded' || state === 'failed' || state === 'cancelled'
}

// Ask a running backup job to stop. The server records the request (any
// worker) and answers 202 at once; the job itself ends 'cancelled' — usually
// within a second or so, but a request made while the server copies the DB
// snapshot (or VACUUMs a copy with statistics / history left out) waits for
// that step, which cannot be interrupted, to finish (minutes on a large DB)
// — or 'succeeded' if the request came too late, after the archive was
// already being published (result.cancel_too_late). 409 (a coded detail:
// adminDash.apiErr.backupCancelFinished / jobNotCancellable /
// jobWorkerExited) when the job has already finished or its worker exited,
// 404 when it is unknown.
export async function adminCancelBackupJob(jobId: string): Promise<void> {
  await adminFetch(
    `/api/admin/backup/jobs/${encodeURIComponent(jobId)}/cancel`,
    'backup cancel failed',
    { method: 'POST' },
  )
}

// Poll a job until it is terminal. A failed poll is retried until
// `maxErrors` fail in a row; a record that is gone or a refused login ends
// the wait at once (thrown). `onPoll` sees every record read on the way;
// `keepLogin` as for adminGetBackupJob.
export async function waitForJob(
  jobId: string,
  opts: {
    intervalMs?: number
    maxErrors?: number
    signal?: AbortSignal
    keepLogin?: boolean
    onPoll?: (job: BackupJob) => void
  } = {},
): Promise<BackupJob> {
  const { intervalMs = 1000, maxErrors = 3, signal, keepLogin, onPoll } = opts
  let errors = 0
  // eslint-disable-next-line no-constant-condition
  while (true) {
    try {
      const j = await adminGetBackupJob(jobId, { signal, keepLogin })
      errors = 0
      onPoll?.(j)
      if (isTerminalJobState(j.state)) return j
    } catch (err) {
      if (isJobNotFoundError(err) || err instanceof UnauthorizedError || signal?.aborted) throw err
      errors += 1
      if (errors >= maxErrors) throw err
    }
    await new Promise((res) => setTimeout(res, intervalMs))
  }
}

export async function adminListBackups(
  opts: { signal?: AbortSignal } = {},
): Promise<BackupListResponse> {
  return adminGet('/api/admin/backup', 'backup list failed', { signal: opts.signal, cache: 'no-store' })
}

export async function adminDeleteBackup(id: string): Promise<void> {
  await adminFetch(`/api/admin/backup/${encodeURIComponent(id)}`, 'backup delete failed', { method: 'DELETE' })
}

// Mint a short-lived signed URL, then navigate to it via a synthetic <a>
// click. This uses the browser's native streaming download path, which:
//   * doesn't buffer the whole archive in tab memory (unlike fetch+Blob), and
//   * doesn't trip Chrome's "blob: URL over insecure connection" block that
//     fires for archive MIME types served from plain-HTTP pages.
export async function adminGetBackupDownloadUrl(id: string): Promise<string> {
  const r = await adminFetch(
    `/api/admin/backup/${encodeURIComponent(id)}/download_url`,
    'backup download url failed',
    { method: 'POST' },
  )
  const { url } = (await r.json()) as { url: string }
  return url
}

export async function adminDownloadBackup(item: BackupItem): Promise<void> {
  const url = await adminGetBackupDownloadUrl(item.id)
  triggerDownload(url, `${item.id}.${item.archive_format}`)
}

// Staging a restore (extract + verify) runs as a background job: these start
// it and answer its id; the job's result is a RestoreStageResponse.
// `replaces`: the stage this one replaces (the preview on screen). The server
// removes it inside the new staging job, before that job measures free space,
// and only once the request was accepted.
export async function adminRestoreFromSnapshot(
  snapshotId: string,
  opts: { replaces?: string | null } = {},
): Promise<JobStarted> {
  const form = new FormData()
  form.append('snapshot_id', snapshotId)
  if (opts.replaces) form.append('replaces_stage_id', opts.replaces)
  return adminSend('POST', '/api/admin/restore', form, 'restore stage failed')
}

// Upload an archive to stage. The server answers 202 { job_id } once it has
// received the body; copying it into staging is the job's first step (a full
// staging volume fails the job with adminDash.apiErr.uploadNoSpace - see
// backupJobError). Errors are typed like the fetch-based calls (a
// `{ code, params }` detail is translated by the caller); `opts.signal`
// cancels a multi-GB upload (and stops it on unmount) instead of letting it
// run on. `opts.onSent` is called once the whole body is sent.
export async function adminRestoreFromUpload(
  file: File,
  onProgress?: (percent: number) => void,
  opts: { signal?: AbortSignal; onSent?: () => void; replaces?: string | null } = {},
): Promise<JobStarted> {
  const form = new FormData()
  // Before the file part, so it is read before the (large) body.
  if (opts.replaces) form.append('replaces_stage_id', opts.replaces)
  form.append('file', file)
  return xhrUpload('/api/admin/restore', form, 'restore upload failed', {
    onProgress,
    onSent: opts.onSent,
    signal: opts.signal,
    // The proxy's own error page: the app did not answer (in time), so
    // whether it received the upload - and is staging it - is unknown.
    gatewayError: (status) => new ApiCodeError('adminDash.apiErr.restoreUploadGateway', { status }),
  })
}

export async function adminApplyRestore(
  stageId: string,
  password: string,
): Promise<RestoreApplyResponse> {
  let r: Response
  try {
    r = await fetch('/api/admin/restore/apply', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ stage_id: stageId, confirm_text: 'RESTORE', password }),
    })
  } catch (err) {
    // fetch rejects with a bare TypeError when the connection drops. The server
    // may already have accepted the apply and be restarting, so callers need to
    // tell this "outcome unknown" case from a definite rejection.
    if (err instanceof TypeError) throw new NetworkError()
    throw err
  }
  // Gateway errors while the app restarts into the apply are the same story:
  // the request may have been accepted before the proxy lost the backend.
  if (isGatewayFailure(r)) throw new NetworkError()
  // r.ok covers 200-299 (including the production-path 202 response).
  if (!r.ok) await throwRestoreApi(r, 'restore apply failed')
  return r.json()
}

async function throwRestoreApi(r: Response, fallback: string): Promise<never> {
  if ((r.headers.get('content-type') || '').includes('application/json')) {
    let detail: any = null
    try { detail = (await r.clone().json())?.detail } catch { /* fall through */ }
    if (
      detail && typeof detail === 'object'
      && typeof detail.code === 'string' && typeof detail.message === 'string'
    ) {
      throw new RestoreRefusedError(detail.code, detail.message)
    }
  }
  return throwApi(r, fallback)
}

export async function adminGetRestoreResult(
  opts: { signal?: AbortSignal } = {},
): Promise<RestoreResult> {
  return adminGet('/api/admin/restore/result', 'restore result failed', { signal: opts.signal, cache: 'no-store' })
}

export async function adminClearRestoreResult(): Promise<void> {
  await adminFetch('/api/admin/restore/result', 'restore result clear failed', { method: 'DELETE' })
}

// Pull the "what's about to happen" summary for a staged restore: archive
// integrity + live state + archive state. Used by the UI to populate the
// pre-confirmation warning panel so the operator can see exactly what gets
// deleted before they click through.
export async function adminGetRestorePreflight(
  stageId: string,
  opts: { signal?: AbortSignal } = {},
): Promise<RestorePreflightResponse> {
  return adminGet(
    `/api/admin/restore/preflight/${encodeURIComponent(stageId)}`,
    'restore preflight failed',
    { signal: opts.signal, cache: 'no-store' },
  )
}

// Reset is "wipe content data, keep admin/config rows". The preflight call
// reports what's currently in the live install so the UI can render an
// honest "this much will be deleted" warning before the destructive POST.
export async function adminGetResetPreflight(
  opts: { signal?: AbortSignal } = {},
): Promise<ResetPreflightResponse> {
  return adminGet('/api/admin/reset/preflight', 'reset preflight failed', { signal: opts.signal, cache: 'no-store' })
}

// Starts the reset job (build the reset stage, then apply it like a restore).
// A dropped connection or a gateway error leaves it unknown whether the job
// started: NetworkError, as for adminApplyRestore.
export async function adminApplyReset(password: string): Promise<ResetStarted> {
  let r: Response
  try {
    r = await fetch('/api/admin/reset', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ confirm_text: 'RESET', password }),
    })
  } catch (err) {
    if (err instanceof TypeError) throw new NetworkError()
    throw err
  }
  if (isGatewayFailure(r)) throw new NetworkError()
  if (!r.ok) await throwApi(r, 'reset failed')
  return r.json()
}

// Drop a staged-but-not-yet-applied restore tree on the server so a
// SCOPE_FULL stage doesn't pin many TB of disk until the 24h sweep TTL
// fires. Idempotent: a missing / already-swept stage still resolves.
export async function adminCancelRestoreStage(stageId: string): Promise<void> {
  await adminFetch(
    `/api/admin/restore/stage/${encodeURIComponent(stageId)}`,
    'restore stage cancel failed',
    { method: 'DELETE' },
  )
}

// One parked `.pre-restore-*` rollback tree under DATA_DIR. A restore / reset
// apply parks the replaced live data here as an automatic-rollback artefact;
// for a full-scope apply that is a complete copy of the WSI corpus, so these
// accumulate and are surfaced for manual reclamation.
export type RollbackDir = {
  name: string
  // ISO-8601 (UTC, "Z") derived from the dir name; "" if unparseable.
  created_at: string
  size_bytes: number
}

export type RollbackListResponse = {
  items: RollbackDir[]
  data_dir: string
}

export async function adminListRollbacks(
  opts: { signal?: AbortSignal } = {},
): Promise<RollbackListResponse> {
  return adminGet('/api/admin/restore/rollbacks', 'rollback list failed', { signal: opts.signal, cache: 'no-store' })
}

export async function adminDeleteRollback(name: string): Promise<void> {
  await adminFetch(
    `/api/admin/restore/rollbacks/${encodeURIComponent(name)}`,
    'rollback delete failed',
    { method: 'DELETE' },
  )
}

// Poll /api/admin/restore/result until the result file describes *this*
// apply (matched by stage_id). Plain /api/health polling races: the first
// poll can land on the still-running pre-restart uvicorn, return 200, and
// resolve before the new process has even started, leaving the operator
// looking at a `null` result. Matching by stage_id is unambiguous because
// each restore-apply produces a fresh uuid-derived stage id.
//
// The result endpoint is admin-authenticated, and the restarted service can
// stop accepting this tab's token: the restored database carries its own
// users (and token_version), or the token simply expired during the wait.
// Retrying a 401 until the timeout would then report "the service did not come
// back" for a restore that had succeeded. A 401/403 is an answer from a live
// service, so once the service has been seen going down (the restart), it
// rejects with UnauthorizedError — "back, but log in again to read the
// result" — and it does the same at the timeout if the service is still
// answering 401/403 then. The error's sawOutage says which: at the timeout
// no restart was ever observed, so the caller must not claim one happened.
export class RestoreWaitUnauthorizedError extends UnauthorizedError {
  sawOutage: boolean
  constructor(status: number, sawOutage: boolean) {
    super(status)
    this.sawOutage = sawOutage
  }
}

export async function waitForRestoreApplied(
  expectedStageId: string,
  timeoutMs = 90_000,
  intervalMs = 1500,
): Promise<NonNullable<RestoreResult['result']>> {
  const start = Date.now()
  let sawOutage = false
  let authRejected = 0
  // eslint-disable-next-line no-constant-condition
  while (true) {
    // 0 = no HTTP answer (connection refused / reset mid-restart).
    let status = 0
    let result: RestoreResult['result'] = null
    try {
      const r = await fetch('/api/admin/restore/result', {
        headers: authHeaders(),
        cache: 'no-store',
      })
      status = r.status
      if (r.ok) result = ((await r.json()) as RestoreResult).result
    } catch {
      status = 0
    }
    if (result && result.stage_id === expectedStageId) return result
    if (status === 401 || status === 403) {
      if (sawOutage) throw new RestoreWaitUnauthorizedError(status, true)
      authRejected = status
    } else {
      authRejected = 0
      // nginx answers 502/503/504 while uvicorn is down.
      if (status === 0 || status >= 500) sawOutage = true
    }
    if (Date.now() - start > timeoutMs) {
      // Only reached with sawOutage false: a 401/403 after an outage throws
      // straight away above.
      if (authRejected) throw new RestoreWaitUnauthorizedError(authRejected, sawOutage)
      throw new Error('timed out waiting for restore result after restart')
    }
    await new Promise((res) => setTimeout(res, intervalMs))
  }
}
