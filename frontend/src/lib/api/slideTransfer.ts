// Client for per-slide export / import (cross-tessera migration). Mirrors the
// patterns in backup.ts: background-job polling, XHR upload-with-progress, and
// native-streaming download via a short-lived signed URL. Distinct from
// backup.ts, which drives the destructive whole-DB restore.
import {
  adminFetch,
  adminGet,
  adminSend,
  ApiCodeError,
  xhrUpload,
} from './shared'
import { triggerDownload } from '../clipboard'

export type TransferJob = {
  id: string
  kind: string
  state: 'pending' | 'running' | 'succeeded' | 'failed' | string
  created_at: string
  // phase: export "queued" / "writing_wsi" / "finalizing"; import staging
  // "copying" (the received bundle into staging: bytes_copied / total_bytes)
  // / "extracting" / "checking"; apply "queued" / "importing"; retry-assets
  // "copying_assets"; "done" once finished.
  progress: {
    phase?: string
    bytes_written?: number
    bytes_copied?: number
    total_bytes?: number
    done?: number
    total?: number
  }
  error: string | null
  result: Record<string, any>
}

// Lightweight per-slide summary carried in the export history so an admin can
// tell two same-count bundles apart without downloading either. Empty when
// the bundle's sidecar is missing or unreadable.
export type ExportSlideSummary = {
  public_id: string
  title: string
  catalog_id: string
}

export type ExportResult = {
  export_id: string
  size_bytes: number
  sha256: string
  created_at: string
  archive_format: string
  slide_count: number
  tour_count: number
  note?: string
  slides: ExportSlideSummary[]
}

// A generated bundle in the export history (same shape the export job returns).
export type ExportListItem = ExportResult

export type ImportPlanSlide = {
  public_id: string
  catalog_id: string
  title: string
  tour_count: number
  wsi_original_basename: string
  wsi_present_in_bundle: boolean
  filename_conflict: boolean
  // A slide on this system already carries this public_id — almost always
  // because this bundle (or another export of the same slide) was imported
  // before. "Import as copy" would add a second, published duplicate under
  // a new id, so the dashboard defaults such a slide to "skip".
  public_id_conflict: boolean
  // Advisory only: the bundle's catalog_id already exists on a target slide.
  // The import still succeeds (catalog_id has no uniqueness constraint) — this
  // just warns the operator that a duplicate catalog number will result.
  catalog_id_conflict: boolean
  // Values the editors would refuse (a control character in the title, an
  // over-long tour description, a non-finite step coordinate, ...): applying
  // fails this slide. `tour` is '' for the slide's own fields; `step` is 0
  // for a field of the tour itself. At most five are listed;
  // content_error_count is the total.
  content_errors: { tour: string; step: number; detail: string }[]
  content_error_count: number
  // Attachments this system's per-file / per-tour limits will refuse (a lower
  // bound: content checks only run at apply time).
  assets_over_limit: number
  // Image-credit fields (credit_text / license_id / source_url) whose value
  // this system does not accept: they are imported empty.
  credit_skipped: string[]
  // What an earlier apply of this (kept) stage did with the slide: imported
  // (only while that slide still exists — it is not re-importable from this
  // stage), failed (with the recorded reason), skipped, or null when it was
  // never attempted. Always null on a freshly uploaded bundle.
  apply_status?: 'imported' | 'failed' | 'skipped' | null
  failure?: { code: string | null; error: string; params: Record<string, string | number> } | null
}

// What a retry of a kept stage could still do: re-apply for the failed
// slides, the attachment retry for attachments this system's limits or type
// allow-list refused.
export type ImportRetryInfo = {
  failed_slides: number
  skipped_assets: number
}

export type ImportPlan = {
  stage_id: string
  bundle_app_version: string
  // Non-null when the import is blocked (currently only a version mismatch).
  blocking_code: string | null
  blocking_reason: string | null
  slide_count: number
  tour_count: number
  slides: ImportPlanSlide[]
  // slides.json keys this system does not know ("key", "tour.key",
  // "step.key"): the bundle comes from a newer release, and these fields
  // cannot be imported.
  unknown_fields: string[]
  // Set when the WSI files still to import must be copied onto a data volume
  // without room for them and the free-space reserve each copy must leave.
  data_space: { needed_bytes: number; free_bytes: number; reserve_bytes: number } | null
  // True once an apply has run against this stage (it was kept for a retry).
  applied?: boolean
  already_imported_count?: number
  retry?: ImportRetryInfo
}

// The staging job's result, and what GET …/import/stage/{id} returns.
export type ImportStageResponse = {
  stage_id: string
  plan: ImportPlan
}

// Per-slide conflict resolution, keyed by the bundle's slide public_id.
export type ImportResolution = 'copy' | 'skip'

export type ImportedSlide = {
  source_public_id: string
  slide_id: number
  public_id: string
  title: string
  wsi_filename: string
  renamed: boolean
  tours: number
  steps: number
}

// Something the import did not carry over: an attribute, image-credit field
// or attachment the target refused (importAttributeSkipped /
// importCreditSkipped / importAssetSkipped), a step image reference that does
// not resolve on the target (importAssetRefMissing), or a bundle field this
// system does not know (importUnknownField; public_id '' — it concerns the
// whole bundle). Keyed by code for localisation.
export type ImportWarning = {
  public_id: string
  code: 'importAssetSkipped' | 'importAssetRefMissing' | 'importAttributeSkipped'
    | 'importCreditSkipped' | 'importUnknownField' | string
  params: Record<string, string | number>
}

export type ImportResult = {
  imported: ImportedSlide[]
  skipped: { public_id: string; reason: string }[]
  failed: { public_id: string; error: string; code?: string; params?: Record<string, string | number> }[]
  imported_count: number
  skipped_count: number
  failed_count: number
  // Capped list; warning_count is the true total.
  warnings: ImportWarning[]
  warning_count: number
  stage_id?: string
  // Slides an earlier apply of the same stage imported (left alone this time).
  already_imported?: { source_public_id: string; slide_id: number; public_id: string; title: string }[]
  already_imported_count?: number
  // The stage (the uploaded bundle, extracted) is kept on the server — for up
  // to 24 h after this run — when something is left to retry.
  stage_kept: boolean
  retry: ImportRetryInfo
}

// Result of the attachment-only retry (POST …/import/retry-assets).
export type ImportAssetRetryResult = {
  stage_id: string
  copied: { public_id: string; tour: string; name: string }[]
  copied_count: number
  // Retried attachments refused again (a limit still too tight).
  still_skipped_count: number
  warnings: ImportWarning[]
  warning_count: number
  stage_kept: boolean
  retry: ImportRetryInfo
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

export async function adminStartExport(
  slideIds: number[],
  note = '',
): Promise<{ job_id: string }> {
  return adminSend('POST', '/api/admin/slides/export', { slide_ids: slideIds, note }, 'export start failed')
}

async function adminGetTransferJob(
  jobId: string,
  opts: { signal?: AbortSignal } = {},
): Promise<TransferJob> {
  return adminGet(
    `/api/admin/slides/transfer/jobs/${encodeURIComponent(jobId)}`,
    'job poll failed',
    { signal: opts.signal, cache: 'no-store' },
  )
}

// Sleep that also settles (rejecting with an AbortError) the moment the
// supplied signal aborts, so a poll loop doesn't keep ticking for up to an
// hour after the caller has gone away (e.g. an admin switched sidebar tabs
// mid apply-poll and unmounted the section).
function abortableDelay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new DOMException('Aborted', 'AbortError')); return }
    const onAbort = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')) }
    const timer = setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve() }, ms)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

// Poll a transfer job to a terminal state. Resolves with the final job (the
// caller inspects state/result/error); rejects only on an explicit timeout,
// repeated transport failure of the poll itself, or ``opts.signal``
// aborting. The signal lets a component stop the loop on unmount — without
// it the poll survives a tab switch and keeps hitting the server (1 req/s).
//
// No time limit by default: the loop runs for as long as the server reports
// the job pending or running. A large bundle's export, staging or apply can
// outlast any fixed limit such as an hour — the dashboard would then say
// "stopped waiting" while the job carried on, never refresh the export
// history and never start the download. A job the server no longer
// runs is not waited on forever: when the worker running it has exited, the
// job GET itself answers it as 'failed' (result.code
// adminDash.apiErr.jobWorkerExited, which transferJobError translates — see
// JobStore.get_reaping_orphan), which ends the loop like any other failure;
// one the server no longer knows at all (the slot reaper deleted the record
// first, or it was pruned) answers 404, which ends the loop after
// maxConsecutiveErrors polls like any other repeated failure. The one case
// left is a dead worker whose pid the OS has already reused, which reads as
// alive: that record stays 'running' until the store's age backstop prunes
// it (then 404) — pass timeoutMs to bound the wait tighter than that.
export async function pollTransferJob(
  jobId: string,
  onProgress?: (job: TransferJob) => void,
  timeoutMs = Number.POSITIVE_INFINITY,
  intervalMs = 1000,
  opts: { signal?: AbortSignal } = {},
): Promise<TransferJob> {
  const { signal } = opts
  const start = Date.now()
  // Tolerate transient poll failures (a momentary network drop / token blip)
  // rather than aborting a job that is still running — but give up after a few
  // consecutive failures so a genuine outage (e.g. an expired session) fails
  // fast instead of spinning until the timeout. Mirrors the resilience of
  // waitForRestoreApplied in backup.ts.
  let consecutiveErrors = 0
  const maxConsecutiveErrors = 5
  // eslint-disable-next-line no-constant-condition
  while (true) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    try {
      const job = await adminGetTransferJob(jobId, { signal })
      consecutiveErrors = 0
      onProgress?.(job)
      // 'cancelled' never happens for a transfer job today (only backups can
      // be cancelled), but it is terminal: never poll it forever.
      if (job.state === 'succeeded' || job.state === 'failed' || job.state === 'cancelled') return job
    } catch (err) {
      // An abort is terminal, not a transient blip — propagate it immediately
      // rather than retrying maxConsecutiveErrors times first.
      if (signal?.aborted) throw err
      consecutiveErrors += 1
      if (consecutiveErrors >= maxConsecutiveErrors) throw err
    }
    if (Date.now() - start > timeoutMs) {
      // Only with an explicit timeoutMs. The job may well still be running;
      // the caller's copy says so.
      throw new ApiCodeError('adminDash.apiErr.transferPollTimeout')
    }
    await abortableDelay(intervalMs, signal)
  }
}

// The error a failed transfer job stands for: the translatable i18n key the
// backend stores in result.code (with result.params) when it has one, else
// the raw (English) job.error.
export function transferJobError(job: TransferJob, fallback: string): Error {
  const code = job.result?.code
  if (typeof code === 'string') return new ApiCodeError(code, job.result?.params ?? undefined)
  return new Error(job.error || fallback)
}

// List the generated export bundles awaiting download (the export history).
// Newest first; each is reclaimed by the 24h staging sweep unless deleted.
export async function adminListExports(): Promise<ExportListItem[]> {
  const r = await adminFetch('/api/admin/slides/export/history', 'export list failed', { cache: 'no-store' })
  const { items } = (await r.json()) as { items: ExportListItem[] }
  return items
}

// Delete an export bundle early (before the 24h TTL sweep would).
export async function adminDeleteExport(exportId: string): Promise<void> {
  await adminFetch(
    `/api/admin/slides/export/${encodeURIComponent(exportId)}`,
    'export delete failed',
    { method: 'DELETE' },
  )
}

async function adminGetExportDownloadUrl(exportId: string): Promise<string> {
  const r = await adminFetch(
    `/api/admin/slides/export/${encodeURIComponent(exportId)}/download_url`,
    'export download url failed',
    { method: 'POST' },
  )
  const { url } = (await r.json()) as { url: string }
  return url
}

export async function adminDownloadExport(
  exportId: string,
  archiveFormat = 'tar.zst',
): Promise<void> {
  const url = await adminGetExportDownloadUrl(exportId)
  triggerDownload(url, `${exportId}.${archiveFormat}`)
}

// ---------------------------------------------------------------------------
// Import
// ---------------------------------------------------------------------------

// Upload a bundle. The server answers 202 { job_id } once it has received the
// upload and stages it (copy into staging, extract, preflight) in that
// background job: poll it with pollTransferJob; a succeeded job's result is
// an ImportStageResponse.
//
// ``opts.signal`` cancels the upload while the body is still being sent
// (rejecting with an AbortError). Once the body is sent the server is
// already starting the job, so an abort then is ignored and the job id is
// still delivered — the caller remembers it rather than strand the stage.
// ``opts.onSent`` is called at that point.
export async function adminStageSlideImport(
  file: File,
  onProgress?: (percent: number) => void,
  opts: { signal?: AbortSignal; onSent?: () => void } = {},
): Promise<{ job_id: string }> {
  const form = new FormData()
  form.append('file', file)
  return xhrUpload('/api/admin/slides/import', form, 'import upload failed', {
    onProgress,
    onSent: opts.onSent,
    signal: opts.signal,
    keepAfterSent: true,
    // The proxy's own error page (bad gateway / unavailable / timeout): the
    // app never answered, so there is no detail to show.
    gatewayError: (status) => new ApiCodeError('adminDash.apiErr.importUploadGateway', { status }),
  })
}

export async function adminApplySlideImport(
  stageId: string,
  resolutions: Record<string, ImportResolution>,
): Promise<{ job_id: string }> {
  return adminSend('POST', '/api/admin/slides/import/apply', { stage_id: stageId, resolutions }, 'import apply failed')
}

// Re-copy, from a kept stage, the attachments an earlier apply skipped
// because of this system's limits / type allow-list (raised since). Same
// job-slot + polling as apply.
export async function adminRetrySlideImportAssets(stageId: string): Promise<{ job_id: string }> {
  return adminSend(
    'POST',
    '/api/admin/slides/import/retry-assets',
    { stage_id: stageId },
    'import attachment retry failed',
  )
}

// Re-read a stage still on the server (kept for a retry, or staged and not
// yet applied) and its recomputed preflight, where each slide carries what an
// earlier apply did with it.
// Rejects (adminDash.apiErr.importStageMissing) once the stage is gone.
export async function adminGetSlideImportStage(stageId: string): Promise<ImportStageResponse> {
  return adminGet(
    `/api/admin/slides/import/stage/${encodeURIComponent(stageId)}`,
    'import stage read failed',
    { cache: 'no-store' },
  )
}

// Drop a staged import tree — not yet applied, or kept for a retry — so it
// doesn't pin disk until the 24h staging sweep. Idempotent: a
// missing/already-swept stage resolves.
export async function adminCancelSlideImport(stageId: string): Promise<void> {
  await adminFetch(
    `/api/admin/slides/import/stage/${encodeURIComponent(stageId)}`,
    'import stage cancel failed',
    { method: 'DELETE' },
  )
}
