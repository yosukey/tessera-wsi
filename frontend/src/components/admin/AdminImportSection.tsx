import React, { useEffect, useRef, useState, type MutableRefObject } from 'react'
import { useInFlight } from '../../hooks/useInFlight'
import { useUnsavedNavigationGuard } from '../../hooks/useUnsavedNavigationGuard'
import { useI18n } from '../../lib/i18n'
import { apiErrorMessage } from '../../lib/admin/apiError'
import { formatBytes } from '../../lib/admin/validation'
import {
  importResolutionsToSend,
  importRowChoosable,
  initialImportResolutions,
  keptStageActions,
  retryFailedResolutions,
} from '../../lib/admin/importRetry'
import { transferProgressText } from '../../lib/admin/transferProgress'
import { safeSessionGetItem, safeSessionRemoveItem, safeSessionSetItem } from '../../lib/safeStorage'
import { STORAGE_KEYS } from '../../lib/storageKeys'
import { Button } from '../ui'
import PhaseStatus from './PhaseStatus'
import {
  ApiCodeError,
  ImportAssetRetryResult,
  ImportPlanSlide,
  ImportResolution,
  ImportResult,
  ImportStageResponse,
  ImportWarning,
  TransferJob,
  adminApplySlideImport,
  adminCancelSlideImport,
  adminGetSlideImportStage,
  adminRetrySlideImportAssets,
  adminSessionIsCurrent,
  adminStageSlideImport,
  currentAdminSessionEpoch,
  isAbortError,
  pollTransferJob,
  transferJobError,
} from '../../lib/api'

// Admin-only "Import slides" flow (cross-tessera migration). Upload a bundle
// produced by Export elsewhere → the server unpacks and checks it in a
// background job → preflight (version guard + per-slide conflict table) →
// apply additively into the live DB. Unlike whole-DB Restore, this never
// restarts the service and needs no password second factor.
//
// The unpacked bundle (the "stage") lives on the server until it is applied
// or discarded, and when an apply leaves something to retry — a failed slide,
// or an attachment this system's limits refused — for 24 h after the last
// attempt, so the retries run against it instead of a re-upload. The staging
// job id, then the stage id, are remembered for the tab session as soon as
// the server hands them out, so the section can return to the bundle — or
// discard it — after a tab switch or a reload (e.g. around the restart that
// applies a raised attachment limit) instead of leaving it on disk unseen.
type Props = {
  onImported?: () => void
  // Reports whether a bundle's body is still being sent, which unmounting
  // this section cancels, so the dashboard can confirm before leaving the
  // tab.
  onSendingChange?: (sending: boolean) => void
  // Holds the navigation guard's in-app exit while this section is mounted
  // (null otherwise); see AdminUploadSection.
  leaveRef?: MutableRefObject<((go: () => void) => void) | null>
}

type TFn = (k: string, p?: Record<string, string | number>) => string

// The latest finished job: an apply (also a "retry failed slides") or an
// attachment-only retry. Both say whether the stage was kept.
type Outcome =
  | { kind: 'apply'; data: ImportResult }
  | { kind: 'assets'; data: ImportAssetRetryResult }

// The server's blocking_reason is English prose; its blocking_code
// (slide_transfer._import_version_block: VERSION_MISMATCH) maps to a
// localized message. An unknown code falls back to the server's text.
function importBlockingText(
  t: (k: string) => string,
  plan: { blocking_code: string | null; blocking_reason: string | null },
): string {
  if (plan.blocking_code) {
    const key = `adminImport.blockingCode.${plan.blocking_code}`
    const text = t(key)
    if (text !== key) return text
  }
  return plan.blocking_reason || t('adminImport.blockedGeneric')
}

// The editor's label for an image-credit field the import refused.
const CREDIT_FIELD_LABEL_KEYS: Record<string, string> = {
  credit_text: 'adminDash.fieldCreditText',
  license_id: 'adminDash.fieldLicense',
  source_url: 'adminDash.fieldSourceUrl',
}

function creditFieldLabel(t: TFn, field: string | number): string {
  const key = CREDIT_FIELD_LABEL_KEYS[String(field)]
  return key ? t(key) : String(field)
}

// One warning line. An attachment's skip reason is a code of its own
// (slide_transfer._asset_limit_reason / _import_tour_assets), localised before
// it is interpolated, as is a credit field's name; an unknown code falls back
// to the raw value.
function importWarningText(t: TFn, w: ImportWarning): string {
  const params = { ...w.params }
  if (w.code === 'importAssetSkipped') {
    const key = `adminImport.assetSkipReason.${params.reason}`
    const reason = t(key)
    if (reason !== key) params.reason = reason
  }
  if (w.code === 'importCreditSkipped') params.field = creditFieldLabel(t, params.field)
  const key = `adminImport.warning.${w.code}`
  const text = t(key, params)
  return text !== key ? text : w.code
}

// A slide failure: prefer the translatable code the backend attaches. The
// catch-all importSlideFailed says nothing about the cause, so the server's
// (English) reason goes with it, as it does for a failure with no code or an
// unknown one.
function failureText(
  t: TFn,
  f: { code?: string | null; error: string; params?: Record<string, string | number> | null },
): string {
  if (!f.code) return f.error
  if (f.code === 'importSlideFailed' && f.error) {
    return t('adminDash.apiErr.importSlideFailedDetail', { detail: f.error })
  }
  const key = `adminDash.apiErr.${f.code}`
  const text = t(key, f.params ?? {})
  return text === key && f.error ? f.error : text
}

// One refused value of a bundled slide (slide_transfer._bundle_content_errors):
// a step's, a tour's own field (step 0), or the slide's own (no tour).
function contentErrorText(t: TFn, e: { tour: string; step: number; detail: string }): string {
  if (e.step > 0) return t('adminImport.contentErrorItem', { tour: e.tour, step: e.step, detail: e.detail })
  if (e.tour) return t('adminImport.contentErrorTour', { tour: e.tour, detail: e.detail })
  return e.detail
}

// The stage endpoint's 404 / a job that found its stage gone.
function isStageMissing(err: unknown): boolean {
  return err instanceof ApiCodeError && err.code === 'adminDash.apiErr.importStageMissing'
}

// The tone of an apply's result box: a run in which every slide it attempted
// failed is an error, one with some failures a warning, so "Imported 0,
// skipped 0, failed 12" never shows as green.
function applyOutcomeTone(data: ImportResult): 'success' | 'warning' | 'danger' {
  if (data.failed_count <= 0) return 'success'
  return data.imported_count > 0 ? 'warning' : 'danger'
}

// An attachment retry that left attachments behind is not a success either.
function assetOutcomeTone(data: ImportAssetRetryResult): 'success' | 'warning' {
  return data.still_skipped_count > 0 ? 'warning' : 'success'
}

// The tokens define no --color-info-border, hence the fallback.
const noticeStyle = (tone: 'success' | 'warning' | 'danger' | 'info'): React.CSSProperties => ({
  padding: '8px 10px', borderRadius: 'var(--radius-md)',
  background: `var(--color-${tone}-bg)`, color: `var(--color-${tone}-fg)`,
  border: `1px solid var(--color-${tone}-border, var(--color-border))`, fontSize: 'var(--font-size-sm)',
})

// The bundle upload in flight, kept outside the section: once its body is
// sent an upload is not cancelled by the section unmounting (the server is
// staging it), and the job id it is waiting for arrives only with the
// server's answer. A section mounted meanwhile attaches to it — its progress,
// then its staging job — instead of offering a second upload whose job id
// would displace this one's and strand the bundle it stages.
type InFlightUpload = {
  pct: number
  // Its whole body has been sent: from then on unmounting the section that
  // started it no longer cancels it.
  sent: boolean
  onChange: Set<() => void>
  done: Promise<{ jobId: string } | { error: unknown }>
}
let inFlightUpload: InFlightUpload | null = null

function startUpload(file: File, signal: AbortSignal | undefined): InFlightUpload {
  const upload = { pct: 0, sent: false, onChange: new Set() } as InFlightUpload
  const epoch = currentAdminSessionEpoch()
  const changed = () => upload.onChange.forEach((fn) => fn())
  upload.done = adminStageSlideImport(file, (pct) => {
    upload.pct = pct
    changed()
  }, {
    signal,
    onSent: () => {
      upload.sent = true
      changed()
    },
  })
    .then(
      ({ job_id }) => {
        // Signed out while the server was answering: the job is the signed-out
        // admin's, not the next account's to be attached to.
        if (!adminSessionIsCurrent(epoch)) {
          return { error: new DOMException('Signed out', 'AbortError') }
        }
        // Remembered before anything else: the section that sent it may be
        // gone already, and the next mount picks the job up.
        safeSessionSetItem(STORAGE_KEYS.importStageJobId, job_id)
        return { jobId: job_id }
      },
      (error: unknown) => ({ error }),
    )
    .finally(() => { if (inFlightUpload === upload) inFlightUpload = null })
  inFlightUpload = upload
  return upload
}

export default function AdminImportSection({ onImported, onSendingChange, leaveRef }: Props) {
  const { t } = useI18n()
  const fileRef = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  // Cancelling a staged bundle. Kept apart from `busy` (whose labels read
  // "Applying…"), but it locks the same controls: an Apply sent meanwhile
  // would race the DELETE, and the cancel then clears a stage being applied.
  const cancelRequest = useInFlight()
  // Non-null from sending a bundle until the server answers with its job.
  const [uploadPct, setUploadPct] = useState<number | null>(null)
  // The bundle's body is still being sent.
  const [sending, setSending] = useState(false)
  // The server is unpacking and checking an uploaded bundle (staging job).
  const [preparing, setPreparing] = useState(false)
  const [stage, setStage] = useState<ImportStageResponse | null>(null)
  const [resolutions, setResolutions] = useState<Record<string, ImportResolution>>({})
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const [error, setError] = useState('')
  // The stage the server kept for a retry (from the last outcome, or
  // remembered from earlier in this tab session), and the choices the last
  // apply was made with — "Retry failed slides" repeats those.
  const [keptStageId, setKeptStageIdState] = useState<string | null>(
    () => safeSessionGetItem(STORAGE_KEYS.importKeptStageId),
  )
  const lastResolutionsRef = useRef<Record<string, ImportResolution>>({})
  // What the server reports of the job being watched (staging, apply,
  // attachment retry): bytes received, slides done, ... — null between jobs.
  const [progressText, setProgressText] = useState<string | null>(null)
  // The job's phase alongside it: screen readers are told about a new phase,
  // not about every poll's new figures (see PhaseStatus).
  const [progressPhase, setProgressPhase] = useState<string | null>(null)

  // Aborts the upload (while its body is still being sent) and the job-poll
  // loops on unmount. Without this, switching sidebar tabs (which unmounts
  // this section) leaves pollTransferJob polling the server once a second for
  // up to an hour — half the admin rate-limit budget.
  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => {
    const ctrl = new AbortController()
    abortRef.current = ctrl
    // A bundle sent before a tab switch may still be waiting for the
    // server's answer; one uploaded before a tab switch or reload may still
    // be being staged, or have been staged meanwhile: pick it up again.
    const jobId = safeSessionGetItem(STORAGE_KEYS.importStageJobId)
    if (inFlightUpload) void followUpload(inFlightUpload)
    else if (jobId) void watchStageJob(jobId)
    return () => ctrl.abort()
  }, [])

  // Leaving this section while a bundle's body is still being sent cancels
  // the upload (above). The dashboard asks before its own exits (a tab
  // switch, a link, Logout) while it is reported here, the guard before
  // browser Back. A reload or a closed tab loses the upload until the server
  // has answered with its job id (which the next load picks up), so
  // beforeunload asks until then.
  useEffect(() => {
    onSendingChange?.(sending)
  }, [sending, onSendingChange])
  useEffect(() => () => onSendingChange?.(false), [onSendingChange])
  const leave = useUnsavedNavigationGuard(sending, t('adminImport.uploadLeaveConfirm'))
  useEffect(() => {
    if (!leaveRef) return
    leaveRef.current = leave
    return () => {
      if (leaveRef.current === leave) leaveRef.current = null
    }
  }, [leaveRef, leave])
  const awaitingUpload = uploadPct !== null
  useEffect(() => {
    if (!awaitingUpload) return
    const handler = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [awaitingUpload])

  function setKeptStageId(id: string | null) {
    setKeptStageIdState(id)
    if (id) safeSessionSetItem(STORAGE_KEYS.importKeptStageId, id)
    else safeSessionRemoveItem(STORAGE_KEYS.importKeptStageId)
  }

  // The kept stage remembered before a job on another stage took the slot
  // (see runStageJob), or null.
  function takeFallbackStageId(): string | null {
    const id = safeSessionGetItem(STORAGE_KEYS.importKeptStageFallbackId)
    safeSessionRemoveItem(STORAGE_KEYS.importKeptStageFallbackId)
    return id
  }

  // ``stageId`` is gone on the server: offer the stage remembered before it
  // instead, if any. Returns the stage now on offer.
  function forgetStage(stageId: string): string | null {
    const fallback = takeFallbackStageId()
    if (keptStageId === stageId || safeSessionGetItem(STORAGE_KEYS.importKeptStageId) === stageId) {
      const next = fallback && fallback !== stageId ? fallback : null
      setKeptStageId(next)
      return next
    }
    return keptStageId
  }

  // A freshly staged bundle is remembered the moment it exists, so a tab
  // switch or reload can't strand it: the upload view then offers to review
  // or discard it. The kept stage it displaces is parked as the fallback,
  // offered again if this one is discarded or turns out to be gone.
  function rememberStage(stageId: string) {
    const prev = safeSessionGetItem(STORAGE_KEYS.importKeptStageId)
    if (prev && prev !== stageId) safeSessionSetItem(STORAGE_KEYS.importKeptStageFallbackId, prev)
    setKeptStageId(stageId)
  }

  const blocked = !!stage?.plan.blocking_code

  // The poll's onProgress: show what the server reports while the job runs
  // (a multi-GB bundle's staging or apply takes minutes to hours).
  function trackProgress(signal: AbortSignal | undefined) {
    return (job: TransferJob) => {
      if (signal?.aborted) return
      const live = job.state === 'running' || job.state === 'pending'
      setProgressText(live ? transferProgressText(t, job.progress) : null)
      setProgressPhase(live ? job.progress?.phase ?? null : null)
    }
  }
  const applied = !!stage?.plan.applied

  // Poll a staging job to its end and show the preflight it produced.
  async function watchStageJob(jobId: string) {
    const signal = abortRef.current?.signal
    setBusy(true)
    setPreparing(true)
    setError('')
    try {
      const job = await pollTransferJob(jobId, trackProgress(signal), undefined, undefined, { signal })
      safeSessionRemoveItem(STORAGE_KEYS.importStageJobId)
      if (job.state !== 'succeeded') throw transferJobError(job, t('adminImport.stageFailed'))
      const resp = job.result as ImportStageResponse
      rememberStage(resp.stage_id)
      setStage(resp)
      // Default every conflicting slide to "copy" (import as a renamed copy);
      // non-conflicting slides need no entry (the backend defaults to copy).
      setResolutions(initialImportResolutions(resp.plan))
    } catch (err) {
      if (isAbortError(err)) return
      // A job the server no longer knows can't be picked up again. Any other
      // failure of the poll itself (network, time-out) leaves it remembered:
      // the job may still finish, and the next visit picks it up.
      if (err instanceof ApiCodeError && err.code === 'adminDash.apiErr.transferJobMissing') {
        safeSessionRemoveItem(STORAGE_KEYS.importStageJobId)
      }
      setError(apiErrorMessage(err, t))
    } finally {
      if (!signal?.aborted) {
        setBusy(false)
        setPreparing(false)
        setProgressText(null)
        setProgressPhase(null)
      }
    }
  }

  async function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    // Allow re-selecting the same file after a cancel.
    e.target.value = ''
    if (!file) return
    setError('')
    setOutcome(null)
    setStage(null)
    setResolutions({})
    await followUpload(startUpload(file, abortRef.current?.signal))
  }

  // Show an upload's progress until the server answers, then watch the
  // staging job it started.
  async function followUpload(upload: InFlightUpload) {
    const signal = abortRef.current?.signal
    setBusy(true)
    const onChange = () => {
      if (signal?.aborted) return
      setUploadPct(upload.pct)
      setSending(!upload.sent)
    }
    onChange()
    upload.onChange.add(onChange)
    let answer: Awaited<InFlightUpload['done']>
    try {
      answer = await upload.done
    } finally {
      upload.onChange.delete(onChange)
    }
    if (signal?.aborted) return
    setSending(false)
    if ('error' in answer) {
      // Cancelled by an unmount while its body was still being sent (the
      // section that sent it is gone): nothing was staged, nothing to show.
      if (!isAbortError(answer.error)) setError(apiErrorMessage(answer.error, t))
      setBusy(false)
      setUploadPct(null)
      return
    }
    setUploadPct(null)
    await watchStageJob(answer.jobId)
  }

  // Start a stage job, poll it to the end, and show its outcome. A kept
  // stage is remembered (and its retry actions offered); otherwise the
  // server removed it and it is forgotten here too.
  async function runStageJob(
    stageId: string,
    start: () => Promise<{ job_id: string }>,
    kind: Outcome['kind'],
  ): Promise<boolean> {
    setBusy(true)
    setError('')
    const signal = abortRef.current?.signal
    let started = false
    const epoch = currentAdminSessionEpoch()
    try {
      const { job_id } = await start()
      // Signed out while the job was being started: nothing of it is remembered
      // for whoever signs in next (the server runs it to the end regardless).
      if (!adminSessionIsCurrent(epoch)) return false
      started = true
      // Persist the stage as soon as its job runs, not only once this
      // component has watched it finish: switching sidebar tabs (or a reload)
      // during a long apply aborts the poll below, and a stage the server
      // then keeps would otherwise be reachable only by uploading the bundle
      // again. Storage only — the on-screen "kept bundle" offer stays as it
      // is while this component is still watching. The stage it displaces is
      // parked as the fallback: a stage the job ends up removing is forgotten
      // — and the displaced one offered again — when the outcome lands, or
      // after an unmount on the next "Review bundle" (its 404).
      if (keptStageId && keptStageId !== stageId) {
        safeSessionSetItem(STORAGE_KEYS.importKeptStageFallbackId, keptStageId)
      }
      safeSessionSetItem(STORAGE_KEYS.importKeptStageId, stageId)
      const job = await pollTransferJob(job_id, trackProgress(signal), undefined, undefined, { signal })
      if (job.state !== 'succeeded') throw transferJobError(job, t('adminImport.applyFailed'))
      const next = { kind, data: { stage_id: stageId, ...job.result } } as Outcome
      setOutcome(next)
      // Only one kept stage is remembered: a newly kept one replaces it, and
      // a finished one is forgotten — but finishing a different bundle leaves
      // an older kept one on offer.
      const fallback = takeFallbackStageId()
      if (next.data.stage_kept) setKeptStageId(stageId)
      else setKeptStageId(fallback && fallback !== stageId ? fallback : null)
      setStage(null)
      setResolutions({})
      onImported?.()
      return true
    } catch (err) {
      // Unmounted mid-poll: nothing left to surface, and touching state would
      // be a no-op on a dead component.
      if (isAbortError(err)) return false
      // The stage is gone (swept, or discarded from another tab): its retry
      // actions can only fail again, so stop offering them. Any other
      // failure — the job failing mid-apply, the poll giving up on a job that
      // may still be running — can leave the stage on the server, so keep
      // offering it (the fallback stays parked for its 404).
      // (A job that never started persisted nothing.)
      if (!started) {
        if (isStageMissing(err)) forgetStage(stageId)
      } else if (isStageMissing(err)) forgetStage(stageId)
      else setKeptStageId(stageId)
      setError(apiErrorMessage(err, t))
      return false
    } finally {
      if (!signal?.aborted) {
        setBusy(false)
        setProgressText(null)
        setProgressPhase(null)
      }
    }
  }

  async function onApply() {
    if (!stage || blocked) return
    const toSend = importResolutionsToSend(stage.plan, resolutions)
    lastResolutionsRef.current = toSend
    await runStageJob(stage.stage_id, () => adminApplySlideImport(stage.stage_id, toSend), 'apply')
  }

  async function onRetryFailedSlides(stageId: string) {
    const failedIds = outcome?.kind === 'apply' ? outcome.data.failed.map((f) => f.public_id) : []
    const toSend = retryFailedResolutions(failedIds, lastResolutionsRef.current)
    await runStageJob(stageId, () => adminApplySlideImport(stageId, toSend), 'apply')
  }

  async function onRetryAssets(stageId: string | null) {
    if (!stageId) return
    await runStageJob(stageId, () => adminRetrySlideImportAssets(stageId), 'assets')
  }

  // Return to a kept stage: its recomputed preflight marks what the earlier
  // apply imported (not re-importable) and what failed or was skipped.
  async function onResume() {
    if (!keptStageId) return
    setBusy(true)
    setError('')
    try {
      const resp = await adminGetSlideImportStage(keptStageId)
      setOutcome(null)
      setStage(resp)
      setResolutions(initialImportResolutions(resp.plan))
    } catch (err) {
      // Gone (discarded elsewhere, swept, or removed by a job that finished
      // while this section was away): stop offering it. When that hands the
      // offer back to the bundle kept before it, there is nothing to report.
      if (isStageMissing(err) && forgetStage(keptStageId)) return
      setError(apiErrorMessage(err, t))
    } finally {
      setBusy(false)
    }
  }

  async function onDiscard(stageId: string | null) {
    if (!stageId) return
    setBusy(true)
    setError('')
    try {
      await adminCancelSlideImport(stageId)
      forgetStage(stageId)
      if (stage?.stage_id === stageId) {
        setStage(null)
        setResolutions({})
      }
    } catch (err) {
      setError(apiErrorMessage(err, t))
    } finally {
      setBusy(false)
    }
  }

  function onCancel() {
    if (busy) return
    return cancelRequest.run(async () => {
      // A bundle not applied yet is dropped from the server (and forgotten
      // here); a kept stage reviewed via "Review" stays there for a later retry.
      if (stage && !applied) {
        try {
          await adminCancelSlideImport(stage.stage_id)
          forgetStage(stage.stage_id)
        } catch {
          // Still on the server, so it stays remembered: the upload view
          // offers to discard it again (the 24 h sweep reclaims it otherwise).
        }
      }
      setStage(null)
      setResolutions({})
      setError('')
    })
  }

  function setResolution(publicId: string, value: ImportResolution) {
    setResolutions((prev) => ({ ...prev, [publicId]: value }))
  }

  function renderStatusCell(s: ImportPlanSlide) {
    if (s.apply_status === 'imported') {
      return (
        <span style={{ color: 'var(--color-success-fg)' }}>✓ {t('adminImport.statusAlreadyImported')}</span>
      )
    }
    const choosable = importRowChoosable(s)
    return (
      <>
        {choosable ? (
          <select
            value={resolutions[s.public_id] ?? 'copy'}
            disabled={locked || blocked}
            onChange={(e) => setResolution(s.public_id, e.target.value as ImportResolution)}
          >
            <option value="copy">{t('adminImport.conflictCopy')}</option>
            <option value="skip">{t('adminImport.conflictSkip')}</option>
          </select>
        ) : (
          <span style={{ color: 'var(--color-text-subtle)' }}>{t('adminImport.statusNew')}</span>
        )}
        {s.apply_status === 'failed' && s.failure && (
          <div style={{ color: 'var(--color-danger-fg)', fontSize: 'var(--font-size-xs)' }}>
            {t('adminImport.statusFailedEarlier', { reason: failureText(t, s.failure) })}
          </div>
        )}
        {s.apply_status === 'skipped' && (
          <div style={{ color: 'var(--color-text-subtle)', fontSize: 'var(--font-size-xs)' }}>
            {t('adminImport.statusSkippedEarlier')}
          </div>
        )}
      </>
    )
  }

  function renderWarnings(data: { warnings: ImportWarning[]; warning_count: number }) {
    if ((data.warning_count ?? 0) <= 0) return null
    return (
      <div role="status" style={noticeStyle('warning')}>
        <div style={{ fontWeight: 600 }}>
          {t('adminImport.warningsTitle', { count: data.warning_count })}
        </div>
        <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
          {data.warnings.map((w, i) => (
            // A bundle-wide warning (an unknown field) names no slide.
            <li key={i}>{w.public_id ? `${w.public_id}: ` : ''}{importWarningText(t, w)}</li>
          ))}
        </ul>
        {data.warning_count > data.warnings.length && (
          <div>{t('adminImport.warningsTruncated', { shown: data.warnings.length, total: data.warning_count })}</div>
        )}
      </div>
    )
  }

  // What the latest outcome left to retry — only while its stage is still
  // the kept one (a discard clears it).
  const outcomeStageId = outcome?.data.stage_id ?? null
  const actions = outcome && outcomeStageId && outcomeStageId === keptStageId
    ? keptStageActions(outcome.data)
    : null
  const locked = busy || cancelRequest.busy

  return (
    <div style={{ display: 'grid', gap: 12 }}>
      <div>
        <h3 style={{ margin: '0 0 4px' }}>{t('adminImport.title')}</h3>
        <p style={{ margin: 0, fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)' }}>
          {t('adminImport.intro')}
        </p>
      </div>

      {error && (
        <div role="alert" style={noticeStyle('danger')}>
          {error}
        </div>
      )}

      {!stage && !outcome && (
        <>
          {keptStageId && (
            <div role="status" style={{ ...noticeStyle('info'), display: 'grid', gap: 8 }}>
              <div>{t('adminImport.keptEarlierNotice')}</div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Button variant="secondary" disabled={locked} onClick={onResume}>
                  {t('adminImport.resume')}
                </Button>
                <Button variant="ghost" disabled={locked} onClick={() => onDiscard(keptStageId)}>
                  {t('adminImport.discardBundle')}
                </Button>
              </div>
            </div>
          )}
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <input
              ref={fileRef}
              type="file"
              accept=".tar.zst,.tar.gz,.tgz"
              style={{ display: 'none' }}
              onChange={onPick}
            />
            <Button variant="primary" disabled={locked} onClick={() => fileRef.current?.click()}>
              {busy && uploadPct != null ? t('adminImport.uploading') : t('adminImport.chooseFile')}
            </Button>
            {uploadPct != null && (
              <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)' }}>
                {t('adminImport.uploadingPct', { pct: uploadPct })}
              </span>
            )}
            {preparing && (
              <PhaseStatus
                text={progressText ?? t('adminImport.preparing')}
                phase={progressPhase ?? 'preparing'}
                style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)' }}
              />
            )}
          </div>
        </>
      )}

      {stage && (
        <div style={{ display: 'grid', gap: 10 }}>
          <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)' }}>
            {t('adminImport.bundleSummary', {
              slides: stage.plan.slide_count,
              tours: stage.plan.tour_count,
              version: stage.plan.bundle_app_version,
            })}
          </div>

          {applied && (
            <div role="status" style={noticeStyle('info')}>
              {t('adminImport.appliedNotice', { count: stage.plan.already_imported_count ?? 0 })}
            </div>
          )}

          {blocked && (
            <div role="alert" style={noticeStyle('warning')}>
              {importBlockingText(t, stage.plan)}
            </div>
          )}

          {(stage.plan.unknown_fields ?? []).length > 0 && (
            <div role="status" style={noticeStyle('warning')}>
              {t('adminImport.unknownFields', { fields: stage.plan.unknown_fields.join(', ') })}
            </div>
          )}

          {stage.plan.data_space && (
            <div role="status" style={noticeStyle('warning')}>
              {t('adminImport.dataSpaceShort', {
                needed: formatBytes(stage.plan.data_space.needed_bytes),
                free: formatBytes(stage.plan.data_space.free_bytes),
                reserve: formatBytes(stage.plan.data_space.reserve_bytes),
              })}
            </div>
          )}

          <div style={{ overflowX: 'auto' }}>
            <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: 'var(--font-size-sm)' }}>
              <thead>
                <tr style={{ textAlign: 'left', color: 'var(--color-text-muted)' }}>
                  <th style={{ padding: '4px 8px' }}>{t('adminImport.colSlide')}</th>
                  <th style={{ padding: '4px 8px' }}>{t('adminImport.colFile')}</th>
                  <th style={{ padding: '4px 8px' }}>{t('adminImport.colTours')}</th>
                  <th style={{ padding: '4px 8px' }}>{t('adminImport.colStatus')}</th>
                </tr>
              </thead>
              <tbody>
                {stage.plan.slides.map((s) => (
                  <tr key={s.public_id} style={{ borderTop: '1px solid var(--color-border)' }}>
                    <td style={{ padding: '4px 8px' }}>
                      <span style={{ fontWeight: 600 }}>{s.title || s.public_id}</span>
                      {s.catalog_id && (
                        <span style={{ color: 'var(--color-text-faint)' }}> · {s.catalog_id}</span>
                      )}
                      {s.public_id_conflict && s.apply_status !== 'imported' && (
                        <span
                          title={t('adminImport.publicIdConflictHint')}
                          style={{ color: 'var(--color-warning-fg)', fontSize: 'var(--font-size-xs)', marginLeft: 6 }}
                        >
                          ⚠ {t('adminImport.publicIdConflict')}
                        </span>
                      )}
                      {s.catalog_id_conflict && s.apply_status !== 'imported' && (
                        <span
                          title={t('adminImport.catalogConflictHint')}
                          style={{ color: 'var(--color-warning-fg)', fontSize: 'var(--font-size-xs)', marginLeft: 6 }}
                        >
                          ⚠ {t('adminImport.catalogConflict')}
                        </span>
                      )}
                    </td>
                    <td style={{ padding: '4px 8px', color: 'var(--color-text-subtle)' }}>
                      {s.wsi_original_basename}
                      {!s.wsi_present_in_bundle && (
                        <span style={{ color: 'var(--color-danger-fg)' }}> · {t('adminImport.wsiMissing')}</span>
                      )}
                      {s.content_error_count > 0 && (
                        <div
                          title={s.content_errors.map((e) => contentErrorText(t, e)).join('\n')}
                          style={{ color: 'var(--color-danger-fg)', fontSize: 'var(--font-size-xs)' }}
                        >
                          ⚠ {t('adminImport.contentErrors', { count: s.content_error_count })}
                        </div>
                      )}
                      {s.assets_over_limit > 0 && s.apply_status !== 'imported' && (
                        <div style={{ color: 'var(--color-warning-fg)', fontSize: 'var(--font-size-xs)' }}>
                          ⚠ {t('adminImport.assetsOverLimit', { count: s.assets_over_limit })}
                        </div>
                      )}
                      {(s.credit_skipped ?? []).length > 0 && s.apply_status !== 'imported' && (
                        <div style={{ color: 'var(--color-warning-fg)', fontSize: 'var(--font-size-xs)' }}>
                          ⚠ {t('adminImport.creditSkipped', {
                            fields: s.credit_skipped.map((f) => creditFieldLabel(t, f)).join(', '),
                          })}
                        </div>
                      )}
                    </td>
                    <td style={{ padding: '4px 8px' }}>{s.tour_count}</td>
                    <td style={{ padding: '4px 8px' }}>{renderStatusCell(s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Button variant="primary" disabled={locked || blocked} onClick={onApply}>
              {busy ? t('adminImport.applying') : applied ? t('adminImport.retryApplyBtn') : t('adminImport.applyBtn')}
            </Button>
            {applied && (stage.plan.retry?.skipped_assets ?? 0) > 0 && (
              <Button variant="secondary" disabled={locked || blocked} onClick={() => onRetryAssets(stage.stage_id)}>
                {t('adminImport.retrySkippedAssets', { count: stage.plan.retry?.skipped_assets ?? 0 })}
              </Button>
            )}
            {applied && (
              <Button variant="ghost" disabled={locked} onClick={() => onDiscard(stage.stage_id)}>
                {t('adminImport.discardBundle')}
              </Button>
            )}
            <Button variant="ghost" disabled={locked} onClick={onCancel}>
              {applied ? t('adminImport.close') : t('adminImport.cancel')}
            </Button>
          </div>
          {busy && progressText && (
            <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)' }}>
              <PhaseStatus text={progressText} phase={progressPhase ?? ''} />
            </div>
          )}
        </div>
      )}

      {outcome && (
        <div style={{ display: 'grid', gap: 8 }}>
          {outcome.kind === 'apply' ? (
            <>
              <div role="status" style={noticeStyle(applyOutcomeTone(outcome.data))}>
                {t('adminImport.resultSummary', {
                  imported: outcome.data.imported_count,
                  skipped: outcome.data.skipped_count,
                  failed: outcome.data.failed_count,
                })}
                {(outcome.data.already_imported_count ?? 0) > 0 && (
                  <> {t('adminImport.alreadyImportedSummary', { count: outcome.data.already_imported_count ?? 0 })}</>
                )}
              </div>
              {outcome.data.failed.length > 0 && (
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 'var(--font-size-sm)', color: 'var(--color-danger-fg)' }}>
                  {outcome.data.failed.map((f, i) => (
                    <li key={i}>{f.public_id}: {failureText(t, f)}</li>
                  ))}
                </ul>
              )}
            </>
          ) : (
            <div role="status" style={noticeStyle(assetOutcomeTone(outcome.data))}>
              {t('adminImport.assetRetrySummary', {
                copied: outcome.data.copied_count,
                still: outcome.data.still_skipped_count,
              })}
            </div>
          )}
          {renderWarnings(outcome.data)}
          {actions && outcomeStageId && (
            <div role="status" style={{ ...noticeStyle('info'), display: 'grid', gap: 8 }}>
              <div>{t('adminImport.keptNotice')}</div>
              {actions.skipped_assets > 0 && <div>{t('adminImport.retryAssetsHint')}</div>}
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {actions.failed_slides > 0 && (
                  <Button variant="primary" disabled={locked} onClick={() => onRetryFailedSlides(outcomeStageId)}>
                    {t('adminImport.retryFailedSlides', { count: actions.failed_slides })}
                  </Button>
                )}
                {actions.skipped_assets > 0 && (
                  <Button variant="secondary" disabled={locked} onClick={() => onRetryAssets(outcomeStageId)}>
                    {t('adminImport.retrySkippedAssets', { count: actions.skipped_assets })}
                  </Button>
                )}
                <Button variant="ghost" disabled={locked} onClick={() => onDiscard(outcomeStageId)}>
                  {t('adminImport.discardBundle')}
                </Button>
              </div>
              {busy && (
                <div>
                  <PhaseStatus text={progressText ?? t('adminImport.retrying')} phase={progressPhase ?? 'retrying'} />
                </div>
              )}
            </div>
          )}
          <div>
            {/* A kept stage stays on the server; the upload view offers it again. */}
            <Button variant="secondary" disabled={locked} onClick={() => setOutcome(null)}>
              {t('adminImport.importAnother')}
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
