import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ExportResult,
  TransferJob,
  adminDownloadExport,
  adminStartExport,
  isAbortError,
  pollTransferJob,
  transferJobError,
} from '../api'

// Driver for the slide list's bulk "Export selected" action: start the
// background export job, poll it to completion, then trigger the
// native-streaming download of the produced bundle. `exporting` drives the
// button's busy state, `progress` what the server reports of the job while it
// runs (bytes written so far; see lib/admin/transferProgress). Rethrows on
// failure so the caller can surface a localized message (the backend enforces
// a single transfer job, so a second concurrent export rejects with 409).
//
// Unmounting the caller (leaving the dashboard) stops the poll, so it does
// not go on polling once a second — and then start the download on whatever
// page the admin has moved to. A stopped export resolves quietly, without the
// download: the bundle is still listed in the export history.
export function useSlideExport() {
  const [exporting, setExporting] = useState(false)
  const [progress, setProgress] = useState<TransferJob['progress'] | null>(null)
  // Reentry guard. It has to be a ref, not `exporting`: the state update is
  // async, so the guard has to hold within a single tick, before React has
  // re-rendered with the busy state.
  const runningRef = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => {
    const ctrl = new AbortController()
    abortRef.current = ctrl
    return () => ctrl.abort()
  }, [])

  const exportSlides = useCallback(async (slideIds: number[], note = '') => {
    if (!slideIds.length) return
    // The button is already disabled off `exporting` while a job runs, so
    // the reachable reentry is a double-click landing two calls in the same
    // tick, before that state has flushed. Without this guard the second call
    // would be rejected by the backend's single-transfer-job rule and then clear
    // the busy flag in its own `finally` — re-enabling the button while the
    // first job was still running, and surfacing a 409 for a job that was
    // fine. Returning early matches the `!slideIds.length` guard above: the
    // caller only bumps a refetch signal on return, so a no-op is inert.
    if (runningRef.current) return
    const signal = abortRef.current?.signal
    if (signal?.aborted) return
    runningRef.current = true
    setExporting(true)
    try {
      const { job_id } = await adminStartExport(slideIds, note)
      // No time limit: a large export can run for hours (pollTransferJob).
      const job = await pollTransferJob(
        job_id,
        (j) => { if (!signal?.aborted) setProgress(j.state === 'running' ? j.progress : null) },
        undefined,
        undefined,
        { signal },
      )
      if (job.state !== 'succeeded') throw transferJobError(job, 'export failed')
      if (signal?.aborted) return
      const res = job.result as ExportResult
      await adminDownloadExport(res.export_id, res.archive_format)
    } catch (err) {
      if (isAbortError(err) && signal?.aborted) return
      throw err
    } finally {
      runningRef.current = false
      if (!signal?.aborted) {
        setExporting(false)
        setProgress(null)
      }
    }
  }, [])

  return { exportSlides, exporting, progress }
}
