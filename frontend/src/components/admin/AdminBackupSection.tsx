import React, { useEffect, useRef, useState } from 'react'
import {
  BackupEstimate,
  BackupItem,
  BackupJob,
  BackupListResponse,
  ResetPreflightResponse,
  RestorePreflightResponse,
  RestoreStageResponse,
  RollbackListResponse,
  SlideFileStatus,
  adminApplyReset,
  adminApplyRestore,
  adminCancelBackupJob,
  adminCancelRestoreStage,
  adminDeleteRollback,
  adminListRollbacks,
  adminClearRestoreResult,
  adminCreateBackup,
  adminDeleteBackup,
  adminDownloadBackup,
  adminGetBackupEstimate,
  adminGetActiveJob,
  adminVerifyBackup,
  adminGetBackupJob,
  adminGetMyIp,
  adminGetResetPreflight,
  adminGetRestorePreflight,
  adminGetRestoreResult,
  adminGetSlideFileStatus,
  adminListBackups,
  adminRestoreFromSnapshot,
  adminRestoreFromUpload,
  backupJobError,
  adminSessionIsCurrent,
  clearToken,
  currentAdminSessionEpoch,
  isAbortError,
  isApiCodeError,
  isJobNotFoundError,
  isNetworkError,
  isRestoreRefusedError,
  isTerminalJobState,
  isUnauthorizedError,
  RestoreWaitUnauthorizedError,
  waitForJob,
  waitForRestoreApplied,
} from '../../lib/api'
import { Badge, Button, Card, Input, LockFieldset, Textarea } from '../ui'
import { useDialogFocus } from '../../hooks/useDialogFocus'
import { useInFlight } from '../../hooks/useInFlight'
import { useI18n } from '../../lib/i18n'
import { apiErrorMessage } from '../../lib/admin/apiError'
import { APP_INFO } from '../../lib/appInfo'
import { STORAGE_KEYS } from '../../lib/storageKeys'
import { formatBytes } from '../../lib/admin/validation'
import type { TFn } from '../../lib/admin/validation'
import { safeSessionGetItem, safeSessionSetItem, safeSessionRemoveItem } from '../../lib/safeStorage'

function formatDateTime(iso: string): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (isNaN(d.getTime())) return iso
    return d.toLocaleString()
  } catch {
    return iso
  }
}

function shortSha(s: string): string {
  return s ? s.slice(0, 12) : ''
}

// The server's blocking_reason is English prose; its blocking_code
// (backup.plan_restore: NO_VERSION / DOWNGRADE_NOT_SUPPORTED /
// SCHEMA_TOO_OLD) maps to a
// localized message. An unknown code falls back to the server's text.
function restoreBlockingText(
  t: (k: string) => string,
  plan: { blocking_code: string | null; blocking_reason: string | null },
): string {
  const reason = plan.blocking_reason || ''
  if (!plan.blocking_code) return reason
  const key = `adminBackup.restore.blockingCode.${plan.blocking_code}`
  const text = t(key)
  return text === key ? reason : text
}

// The error of a refused restore apply, localized where its code is known.
function restoreApplyErrorText(t: (k: string) => string, err: unknown): string {
  if (isRestoreRefusedError(err)) {
    return restoreBlockingText(t, { blocking_code: err.code, blocking_reason: err.message })
  }
  return apiErrorMessage(err, t) || t('adminBackup.error.generic')
}

// Why a restore staging (or backup) job failed, localized where the server
// gave a code (a staging volume that filled up while the upload was copied
// into it; a job whose server worker exited).
function restoreJobErrorText(t: (k: string) => string, job: BackupJob): string {
  return apiErrorMessage(backupJobError(job, ''), t) || t('adminBackup.error.generic')
}

// What a succeeded backup left out, as display lines. The server sends each
// warning twice, index-aligned: result.warnings (English sentences, also what
// its log says) and result.warning_items ({code, params}; see
// backup._BackupWarnings). A coded item renders through
// adminBackup.warning.<code> in the admin's language; an unknown code (a
// newer server) or an uncoded item shows the sentence at the same index, so
// nothing is ever dropped.
export function backupWarningLines(t: TFn, result: BackupJob['result'] | undefined): string[] {
  const raw = result?.warnings
  const sentences: string[] = Array.isArray(raw)
    ? raw.map((w) => (typeof w === 'string' ? w : ''))
    : []
  const items = Array.isArray(result?.warning_items) ? result.warning_items : []
  const n = Math.max(sentences.length, items.length)
  const lines: string[] = []
  for (let i = 0; i < n; i += 1) {
    const item = items[i]
    const code = item && typeof item.code === 'string' ? item.code : null
    if (code) {
      const key = `adminBackup.warning.${code}`
      const params = item.params && typeof item.params === 'object' ? item.params : {}
      const text = t(key, params)
      if (text !== key) { lines.push(text); continue }
    }
    if (sentences[i]) lines.push(sentences[i])
  }
  return lines
}

// A job known only by id until its first poll answers.
function placeholderJob(
  id: string,
  kind: BackupJob['kind'],
  progress: BackupJob['progress'],
): BackupJob {
  return { id, kind, state: 'pending', created_at: '', progress, error: null, result: {} }
}

type JobPollEvent =
  | { kind: 'update'; job: BackupJob }
  // The record is gone (pruned, job store cleared): no outcome will come.
  | { kind: 'lost' }
  | { kind: 'error'; error: unknown; consecutive: number }

// Poll an active (pending / running) job every second until it is terminal or
// lost, handing every outcome to onEvent. Self-rescheduling after each poll
// resolves (not setInterval), so a slow poll can't overlap the next and let a
// stale 'running' snapshot overwrite a newer one.
function useJobPolling(job: BackupJob | null, onEvent: (e: JobPollEvent) => void | Promise<void>) {
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent
  const jobId = job?.id
  const active = !!job && (job.state === 'pending' || job.state === 'running')
  useEffect(() => {
    if (!jobId || !active) return
    const ctrl = new AbortController()
    let stopped = false
    let timer: ReturnType<typeof setTimeout> | null = null
    let consecutive = 0
    const tick = async () => {
      try {
        const j = await adminGetBackupJob(jobId, { signal: ctrl.signal })
        consecutive = 0
        await onEventRef.current({ kind: 'update', job: j })
        if (isTerminalJobState(j.state)) return
      } catch (err) {
        if (isAbortError(err)) return
        if (isJobNotFoundError(err)) {
          await onEventRef.current({ kind: 'lost' })
          return
        }
        consecutive += 1
        await onEventRef.current({ kind: 'error', error: err, consecutive })
      }
      if (!stopped) timer = setTimeout(tick, 1000)
    }
    timer = setTimeout(tick, 1000)
    return () => { stopped = true; ctrl.abort(); if (timer) clearTimeout(timer) }
  }, [jobId, active])
}

function ComponentToggle(props: {
  checked: boolean
  onChange: () => void
  disabled: boolean
  label: string
  help: string
  sizeBytes: number | null
}): React.ReactElement {
  return (
    <label style={{ display: 'flex', gap: 6, alignItems: 'flex-start', cursor: 'pointer' }}>
      <input
        type="checkbox"
        checked={props.checked}
        onChange={props.onChange}
        disabled={props.disabled}
        style={{ marginTop: 2 }}
      />
      <span style={{ flex: 1 }}>
        <div style={{ fontWeight: 600 }}>
          {props.label}
          {props.sizeBytes != null && (
            <span style={{ marginLeft: 8, fontWeight: 400, fontFamily: 'monospace', color: 'var(--color-text-muted)' }}>
              {formatBytes(props.sizeBytes)}
            </span>
          )}
        </div>
        <div style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>{props.help}</div>
      </span>
    </label>
  )
}

// One line in the saved-backup list "scope" column: a component name with an
// included / excluded marker. Renders all three components for every row so the
// list always shows the full picture (WSI + analytics + history) rather than
// only flagging what's missing.
function ComponentStatus(props: {
  included: boolean
  label: string
  title: string
}): React.ReactElement {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        whiteSpace: 'nowrap',
        color: props.included ? 'var(--color-text)' : 'var(--color-text-faint)',
      }}
      title={props.title}
    >
      <span
        aria-hidden="true"
        style={{
          fontWeight: 700,
          color: props.included ? 'var(--color-success, #1a7f37)' : 'var(--color-text-faint)',
        }}
      >
        {props.included ? '✓' : '✕'}
      </span>
      <span style={{ textDecoration: props.included ? 'none' : 'line-through' }}>
        {props.label}
      </span>
    </div>
  )
}

const SECTION: React.CSSProperties = {
  marginTop: 'var(--space-5)',
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--space-4)',
}
const CARD_PAD: React.CSSProperties = { padding: 'var(--space-4)' }
const ROW: React.CSSProperties = { display: 'flex', gap: 'var(--space-3)', alignItems: 'center', flexWrap: 'wrap' }
const ERROR_STYLE: React.CSSProperties = {
  marginTop: 'var(--space-2)',
  padding: 'var(--space-2) var(--space-3)',
  borderRadius: 4,
  background: 'rgba(220, 53, 69, 0.08)',
  color: 'var(--color-danger, #b00020)',
  fontSize: 13,
}
const WARN_STYLE: React.CSSProperties = {
  padding: 'var(--space-2) var(--space-3)',
  borderRadius: 4,
  background: 'rgba(220, 150, 0, 0.1)',
  color: 'var(--color-warning-text, #7d4e00)',
  fontSize: 13,
}
const TABLE_STYLE: React.CSSProperties = {
  width: '100%',
  borderCollapse: 'collapse',
  fontSize: 13,
}
const TH_STYLE: React.CSSProperties = {
  padding: '8px 6px',
  textAlign: 'left',
  borderBottom: '1px solid var(--color-border)',
  fontSize: 11,
  textTransform: 'uppercase',
  letterSpacing: '0.07em',
  color: 'var(--color-text-faint)',
  fontWeight: 700,
}
const TD_STYLE: React.CSSProperties = {
  padding: '8px 6px',
  borderBottom: '1px solid var(--color-border)',
  verticalAlign: 'top',
}

export default function AdminBackupSection() {
  const { t } = useI18n()
  const currentVersion = APP_INFO.version

  // Three selectable backup components, all default-on (a default backup is a
  // full snapshot). include_wsi maps to the metadata/full scope server
  // side; the other two strip their DB table groups when off.
  const [includeWsi, setIncludeWsi] = useState(true)
  const [includeAnalytics, setIncludeAnalytics] = useState(true)
  const [includeHistory, setIncludeHistory] = useState(true)
  const [estimate, setEstimate] = useState<BackupEstimate | null>(null)
  const [note, setNote] = useState('')
  const [backupJob, setBackupJob] = useState<BackupJob | null>(null)
  const [backupError, setBackupError] = useState<string | null>(null)
  // Cancelling the running backup: set once this tab has asked (the job's own
  // cancel_requested takes over from the next poll, and covers a request made
  // from another tab or before a remount); a failed request's message; and
  // the neutral notice shown once the job has ended cancelled (or finished
  // before the request could stop it).
  const [cancelAsked, setCancelAsked] = useState(false)
  const [cancelError, setCancelError] = useState<string | null>(null)
  const [backupNotice, setBackupNotice] = useState<string | null>(null)
  // The create request is in flight: Start stays disabled until it answers,
  // so a quick second confirm cannot send a second request (whose 409 would
  // then show next to the backup that did start).
  const [startingBackup, setStartingBackup] = useState(false)
  // A job this tab did not start holds the slot (neither a backup it follows
  // nor its own staging): named until it ends.
  const [otherJob, setOtherJob] = useState<BackupJob | null>(null)
  const [list, setList] = useState<BackupListResponse | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  // One backup delete at a time; every row's Delete waits for it, so a
  // second click can't send a second DELETE for the same archive.
  const deleteRequest = useInFlight()
  // A verify of one saved backup (adminVerifyBackup) and what the last one found.
  const [verifyJob, setVerifyJob] = useState<BackupJob | null>(null)
  const [verifyingId, setVerifyingId] = useState<string | null>(null)
  const [verifyOutcome, setVerifyOutcome] = useState<{ id: string; ok: boolean; text: string } | null>(null)

  // ---- pre-restore rollback directories (reclaim disk) ----
  const [rollbacks, setRollbacks] = useState<RollbackListResponse | null>(null)
  const [rollbacksError, setRollbacksError] = useState<string | null>(null)
  const [deletingRollback, setDeletingRollback] = useState<string | null>(null)

  const [restoreError, setRestoreError] = useState<string | null>(null)
  const [stagedRestore, setStagedRestore] = useState<RestoreStageResponse | null>(null)
  // The restore staging job (extract + verify on the server) while it runs.
  // Its id is also kept in sessionStorage (restoreStageJobId) until the stage
  // it produces is applied or released, so a remount — sidebar tab switch,
  // reload — resumes the job or shows its staged preview again.
  const [restoreJob, setRestoreJob] = useState<BackupJob | null>(null)
  const [stagingFromId, setStagingFromId] = useState<string | null>(null)
  const [uploadProgress, setUploadProgress] = useState<number | null>(null)
  const [uploadStaging, setUploadStaging] = useState(false)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  // Abort handle for the in-flight restore upload (Cancel button / unmount).
  const uploadCtrlRef = useRef<AbortController | null>(null)
  // False once this section unmounts (sidebar tab switch): an upload that
  // finishes after that only records its job id for the next mount.
  const mountedRef = useRef(true)

  const [confirmOpen, setConfirmOpen] = useState(false)
  const [confirmBackupOpen, setConfirmBackupOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [applying, setApplying] = useState(false)
  const [waitingHealth, setWaitingHealth] = useState(false)
  const [lastResult, setLastResult] = useState<any>(null)
  // Set after a re-login that followed a restore / reset whose outcome this
  // tab could not read: the result file on the server does not (yet) carry
  // that apply's stage_id, so whatever it holds is an earlier restore's.
  const [awaitedResultMissing, setAwaitedResultMissing] = useState(false)

  // Preflight result for the currently-staged archive. Loaded once the
  // stage_id is known; surfaces "what's about to happen" before the
  // destructive confirm dialog.
  const [preflight, setPreflight] = useState<RestorePreflightResponse | null>(null)
  const [preflightLoading, setPreflightLoading] = useState(false)
  const [preflightError, setPreflightError] = useState<string | null>(null)

  // ---- reset feature ----
  const [resetPanelOpen, setResetPanelOpen] = useState(false)
  const [resetPreflight, setResetPreflight] = useState<ResetPreflightResponse | null>(null)
  const [resetPreflightError, setResetPreflightError] = useState<string | null>(null)
  const [resetPreflightLoading, setResetPreflightLoading] = useState(false)
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false)
  const [resetConfirmText, setResetConfirmText] = useState('')
  const [resetPassword, setResetPassword] = useState('')
  const [resetApplying, setResetApplying] = useState(false)
  // The reset job is building its stage (before the restart that applies it).
  const [resetPreparing, setResetPreparing] = useState(false)
  const [resetWaitingHealth, setResetWaitingHealth] = useState(false)
  const [resetError, setResetError] = useState<string | null>(null)

  const [fileStatus, setFileStatus] = useState<SlideFileStatus | null>(null)
  const [fileStatusLoading, setFileStatusLoading] = useState(false)
  const [fileStatusError, setFileStatusError] = useState<string | null>(null)

  const [viaTunnel, setViaTunnel] = useState(false)

  // ---- list refresh ----
  async function refreshList(signal?: AbortSignal): Promise<void> {
    try {
      const data = await adminListBackups({ signal })
      setList(data)
      setListError(null)
    } catch (err) {
      if (!isAbortError(err)) setListError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    }
  }

  async function refreshRollbacks(signal?: AbortSignal): Promise<void> {
    try {
      const data = await adminListRollbacks({ signal })
      setRollbacks(data)
      setRollbacksError(null)
    } catch (err) {
      if (!isAbortError(err)) setRollbacksError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    }
  }

  async function onDeleteRollback(name: string): Promise<void> {
    if (!window.confirm(t('adminBackup.rollback.confirmDelete'))) return
    setDeletingRollback(name)
    try {
      await adminDeleteRollback(name)
      await refreshRollbacks()
    } catch (err) {
      setRollbacksError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    } finally {
      setDeletingRollback(null)
    }
  }

  useEffect(() => {
    const ctrl = new AbortController()
    refreshList(ctrl.signal)
    refreshRollbacks(ctrl.signal)
    // Per-component size hints for the create-backup toggles. Best-effort:
    // a failure just leaves the hints blank, it doesn't block backups.
    adminGetBackupEstimate({ signal: ctrl.signal })
      .then((e) => setEstimate(e))
      .catch((err) => { if (!isAbortError(err)) { /* ignore — hints are best-effort */ } })
    // The outcome of an apply this tab just made, carried across the reload
    // reloadAfterRestore() does. The server's result file (below) supersedes
    // it when present (the in-process dev apply writes one too).
    // reloginAfterRestore() instead carries { awaitingStageId }: the apply's
    // outcome was never read, and only a server result with that stage_id is
    // this apply's.
    const carried = safeSessionGetItem(STORAGE_KEYS.adminPostRestore)
    let awaitingStageId: string | null = null
    if (carried !== null) {
      safeSessionRemoveItem(STORAGE_KEYS.adminPostRestore)
      try {
        const parsed = JSON.parse(carried)
        if (parsed && typeof parsed.awaitingStageId === 'string') awaitingStageId = parsed.awaitingStageId
        else if (parsed) setLastResult(parsed)
      } catch { /* malformed — the server's copy below still applies */ }
    }
    // Also pull any restore result left over from a previous restart.
    adminGetRestoreResult({ signal: ctrl.signal })
      .then((r) => {
        if (awaitingStageId && r.result?.stage_id !== awaitingStageId) setAwaitedResultMissing(true)
        if (r.result) setLastResult(r.result)
      })
      .catch((err) => { if (!isAbortError(err)) { /* ignore */ } })
    adminGetMyIp({ signal: ctrl.signal })
      .then((r) => setViaTunnel(r.via_cloudflare_tunnel))
      .catch((err) => { if (!isAbortError(err)) { /* ignore — banner is best-effort */ } })
    // Re-attach to a backup job still running from before a remount (a sidebar
    // tab switch unmounts this section). The job id otherwise lives only in
    // component state, so a remount would both lose the progress display and
    // re-enable the Start button — letting a second POST kick off a concurrent
    // backup. Resume polling if the persisted job is still active; drop the
    // pointer once the job is terminal or its record is gone.
    const savedJobId = safeSessionGetItem(STORAGE_KEYS.backupJobId)
    if (savedJobId) {
      adminGetBackupJob(savedJobId, { signal: ctrl.signal })
        .then((j) => {
          if (j.state === 'pending' || j.state === 'running') {
            setBackupJob(j)  // drives the polling effect + disables Start
          } else {
            // It ended while this section was unmounted, so the poll that
            // would have reported the ending was torn down with it. Report it
            // now, as that poll would have: the cancel notices (an operator
            // who clicked Cancel and switched tabs otherwise never learns
            // whether it worked, or that it came too late and the archive is
            // in the list after all), a failure's reason, a success's
            // warnings (a terminal job in state renders them and is never
            // polled). Once only: the pointer goes now, so the next remount
            // says nothing.
            safeSessionRemoveItem(STORAGE_KEYS.backupJobId)
            setBackupJob(j)
            if (j.state === 'cancelled') setBackupNotice(t('adminBackup.cancel.done'))
            else if (j.state === 'succeeded' && j.result?.cancel_too_late) setBackupNotice(t('adminBackup.cancel.tooLate'))
            else if (j.state === 'failed') setBackupError(restoreJobErrorText(t, j))
          }
        })
        .catch((err) => {
          if (isAbortError(err)) return
          // Unknown / pruned job id — clear the stale pointer so it doesn't
          // keep firing a doomed poll on every remount.
          if (isJobNotFoundError(err)) { safeSessionRemoveItem(STORAGE_KEYS.backupJobId); return }
          // Otherwise poll it: the poll reports what it finds.
          setBackupJob(placeholderJob(savedJobId, 'backup', {}))
        })
    }
    // A job this tab did not start — another tab, another admin, a reopened
    // browser — holds the slot: follow a backup like one this tab started (progress,
    // Cancel), and say what else is running rather than offer a Start that
    // can only answer 409.
    if (!savedJobId) {
      void attachActiveJob(ctrl.signal)
    }
    // Likewise a restore staging job, or the stage it produced.
    const savedRestoreJobId = safeSessionGetItem(STORAGE_KEYS.restoreStageJobId)
    if (savedRestoreJobId) {
      adminGetBackupJob(savedRestoreJobId, { signal: ctrl.signal })
        .then((j) => {
          if (j.state === 'pending' || j.state === 'running') {
            setRestoreJob(j)
          } else if (j.state === 'succeeded' && j.result?.stage_id) {
            setStagedRestore(j.result as RestoreStageResponse)
          } else {
            safeSessionRemoveItem(STORAGE_KEYS.restoreStageJobId)
            if (j.state === 'failed') setRestoreError(restoreJobErrorText(t, j))
          }
        })
        .catch((err) => {
          if (isAbortError(err)) return
          if (isJobNotFoundError(err)) { safeSessionRemoveItem(STORAGE_KEYS.restoreStageJobId); return }
          setRestoreJob(placeholderJob(savedRestoreJobId, 'restore_stage', {}))
        })
    }
    return () => ctrl.abort()
  }, [])

  // ---- backup job polling ----
  useJobPolling(backupJob, async (e) => {
    if (e.kind === 'update') {
      const j = e.job
      setBackupJob(j)
      // Clear any transient error from a previous tick once polling recovers.
      setBackupError((prev) => (j.state === 'failed' ? prev : null))
      if (isTerminalJobState(j.state)) {
        // Terminal: drop the persisted re-attach pointer so a later remount
        // doesn't resurrect a finished job.
        safeSessionRemoveItem(STORAGE_KEYS.backupJobId)
        setCancelAsked(false)
        setCancelError(null)
        // Neutral, not an error: the operator asked for this. A request that
        // came too late (the archive was already being published) leaves a
        // normal, listed backup — say so, or the list would look wrong.
        if (j.state === 'cancelled') setBackupNotice(t('adminBackup.cancel.done'))
        else if (j.state === 'succeeded' && j.result?.cancel_too_late) setBackupNotice(t('adminBackup.cancel.tooLate'))
        // No abort signal: setBackupJob above ends the polling effect, whose
        // cleanup would cancel the refresh and leave the new backup unlisted.
        await refreshList()
        // result.code when the server gave one (a job whose worker exited:
        // jobWorkerExited), else its (English) error.
        if (j.state === 'failed') setBackupError(restoreJobErrorText(t, j))
      }
    } else if (e.kind === 'lost') {
      // Nothing will ever finish this job: stop waiting and re-enable Start.
      setBackupJob(null)
      setCancelAsked(false)
      setCancelError(null)
      safeSessionRemoveItem(STORAGE_KEYS.backupJobId)
      await refreshList()
      setBackupError(t('adminBackup.error.jobLost'))
    } else if (e.consecutive >= 3) {
      // Tolerate brief blips (e.g. a poll lands on a worker that hasn't seen
      // the latest persisted state yet) before surfacing the error.
      setBackupError(apiErrorMessage(e.error, t) || t('adminBackup.error.generic'))
    }
  })

  // ---- restore staging job polling ----
  useJobPolling(restoreJob, (e) => {
    if (e.kind === 'update') {
      const j = e.job
      if (j.state === 'succeeded') {
        setRestoreJob(null)
        // The pointer stays: a remount shows this preview again.
        if (j.result?.stage_id) setStagedRestore(j.result as RestoreStageResponse)
        else rememberRestoreJob(null)
      } else if (j.state === 'failed') {
        setRestoreJob(null)
        rememberRestoreJob(null)
        setRestoreError(restoreJobErrorText(t, j))
      } else {
        setRestoreJob(j)
        setRestoreError(null)
      }
    } else if (e.kind === 'lost') {
      setRestoreJob(null)
      rememberRestoreJob(null)
      setRestoreError(t('adminBackup.error.jobLost'))
    } else if (e.consecutive >= 3) {
      setRestoreError(apiErrorMessage(e.error, t) || t('adminBackup.error.generic'))
    }
  })

  // Follow the job that holds the slot when this tab did not start it (see
  // the mount effect). True when it was a backup this tab now follows.
  async function attachActiveJob(signal?: AbortSignal): Promise<boolean> {
    let j: BackupJob | null = null
    const epoch = currentAdminSessionEpoch()
    try {
      j = await adminGetActiveJob({ signal })
    } catch {
      return false  // best-effort: the screen works without it
    }
    if (!mountedRef.current || !adminSessionIsCurrent(epoch)) return false
    if (!j) {
      setOtherJob(null)
      return false
    }
    if (j.kind === 'backup') {
      setOtherJob(null)
      setBackupJob(j)
      safeSessionSetItem(STORAGE_KEYS.backupJobId, j.id)
      return true
    }
    // A check of a saved backup is followed like this tab's own (its row
    // shows it running, with Cancel, and the outcome when it ends).
    if (j.kind === 'backup_verify') {
      setOtherJob(null)
      const id = typeof j.progress?.backup_id === 'string' ? j.progress.backup_id : null
      if (id && !verifyJob) {
        setVerifyingId(id)
        setVerifyJob(j)
      }
      return false
    }
    // Restore staging that this tab started is re-attached from its own
    // pointer; anything else is only reported.
    if (j.kind !== 'restore_stage' || safeSessionGetItem(STORAGE_KEYS.restoreStageJobId) !== j.id) {
      setOtherJob(j)
    }
    return false
  }

  // Follow that other job to its end, so its notice does not outlive it.
  useEffect(() => {
    if (!otherJob) return
    const timer = setInterval(() => { void attachActiveJob() }, 5000)
    return () => clearInterval(timer)
  }, [otherJob?.id])

  function onCreate() {
    setBackupError(null)
    setConfirmBackupOpen(true)
  }

  async function startBackupNow() {
    setConfirmBackupOpen(false)
    if (startingBackup) return
    setStartingBackup(true)
    setBackupError(null)
    setBackupNotice(null)
    setCancelAsked(false)
    setCancelError(null)
    const epoch = currentAdminSessionEpoch()
    try {
      const r = await adminCreateBackup(
        {
          include_wsi: includeWsi,
          include_analytics: includeAnalytics,
          include_history: includeHistory,
        },
        note.trim(),
      )
      // Signed out meanwhile: the job is not the next account's to follow.
      if (!adminSessionIsCurrent(epoch)) return
      setBackupJob(placeholderJob(r.job_id, 'backup', {}))
      // Persist so a tab-switch remount can re-attach to this running job.
      safeSessionSetItem(STORAGE_KEYS.backupJobId, r.job_id)
    } catch (err) {
      // Busy: someone else's job holds the slot. A backup is followed (with
      // its Cancel) instead of only saying "busy".
      if (await attachActiveJob()) return
      setBackupError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    } finally {
      setStartingBackup(false)
    }
  }

  // Ask the running backup to stop. The request only records the cancel; the
  // job polling above follows the job to 'cancelled' (or to 'succeeded' when
  // the request lands as the archive is being published).
  async function onCancelBackup() {
    const jobId = backupJob?.id
    if (!jobId) return
    if (!window.confirm(t('adminBackup.cancel.confirm'))) return
    setCancelError(null)
    setCancelAsked(true)
    try {
      await adminCancelBackupJob(jobId)
    } catch (err) {
      // Most likely the job finished meanwhile (409); the next poll shows
      // how. Otherwise the backup keeps running, so the button comes back.
      setCancelAsked(false)
      setCancelError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    }
  }

  async function onDelete(item: BackupItem) {
    if (!window.confirm(t('adminBackup.deleteConfirm', { id: item.id }))) return
    await deleteRequest.run(async () => {
      try {
        await adminDeleteBackup(item.id)
        await refreshList()
      } catch (err) {
        setListError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
      }
    })
  }

  // One stop per check: a second, sent before the job has wound down (the
  // stop is only taken up at its next step), would report it as finished.
  // The job a stop was accepted for, until the check ends.
  const cancelVerifyRequest = useInFlight()
  const [verifyStopAskedFor, setVerifyStopAskedFor] = useState<string | null>(null)
  async function onCancelVerify() {
    const jobId = verifyJob?.id
    if (!jobId) return
    await cancelVerifyRequest.run(async () => {
      try {
        await adminCancelBackupJob(jobId)
        setVerifyStopAskedFor(jobId)
      } catch (err) {
        // Most likely it finished meanwhile; the next poll says how.
        setListError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
      }
    })
  }

  async function onVerify(item: BackupItem) {
    setListError(null)
    setVerifyOutcome(null)
    setVerifyingId(item.id)
    try {
      const r = await adminVerifyBackup(item.id)
      setVerifyJob(placeholderJob(r.job_id, 'backup_verify', {}))
    } catch (err) {
      setVerifyingId(null)
      setListError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    }
  }

  useJobPolling(verifyJob, (e) => {
    const id = verifyingId ?? ''
    if (e.kind === 'update') {
      const j = e.job
      if (!isTerminalJobState(j.state)) { setVerifyJob(j); return }
      setVerifyJob(null)
      setVerifyingId(null)
      if (j.state === 'succeeded') {
        setVerifyOutcome({
          id,
          ok: true,
          text: t(j.result?.sha256_checked ? 'adminBackup.verify.ok' : 'adminBackup.verify.okNoSha', { id }),
        })
      } else if (j.state === 'cancelled') {
        setVerifyOutcome({ id, ok: true, text: t('adminBackup.verify.cancelled', { id }) })
      } else if (j.result?.damaged) {
        setVerifyOutcome({ id, ok: false, text: t('adminBackup.verify.failed', { id, reason: restoreJobErrorText(t, j) }) })
      } else {
        // The check itself failed (an I/O error, its worker exited): that
        // says nothing about the archive, so it is not called damaged.
        setVerifyOutcome({ id, ok: false, text: t('adminBackup.verify.notChecked', { id, reason: restoreJobErrorText(t, j) }) })
      }
    } else if (e.kind === 'lost') {
      setVerifyJob(null)
      setVerifyingId(null)
      setListError(t('adminBackup.error.jobLost'))
    } else if (e.consecutive >= 3) {
      setListError(apiErrorMessage(e.error, t) || t('adminBackup.error.generic'))
    }
  })

  async function onDownload(item: BackupItem) {
    try {
      await adminDownloadBackup(item)
    } catch (err) {
      setListError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    }
  }

  // The restore already staged, set aside while a new one is started: its
  // preview leaves the screen at once (the single stagedRestore slot is the
  // new one's), but the stage itself stays until the server has accepted the
  // new staging job, which then removes it (`replaces`) before measuring free
  // space. Released by this page up front, a refused attempt (busy 409, a
  // failed or cancelled upload) would lose the archive already staged;
  // released after acceptance, the new job would already have counted its
  // space against the volume and could fail for want of it — losing both.
  function holdPriorStage(): { stageId: string | null; restore: () => void } {
    const prior = stagedRestore
    const priorJobId = safeSessionGetItem(STORAGE_KEYS.restoreStageJobId)
    setStagedRestore(null)
    return {
      stageId: prior?.stage_id ?? null,
      restore: () => {
        if (!prior || !mountedRef.current) return
        setStagedRestore(prior)
        if (priorJobId) rememberRestoreJob(priorJobId)
      },
    }
  }

  function rememberRestoreJob(jobId: string | null) {
    if (jobId) safeSessionSetItem(STORAGE_KEYS.restoreStageJobId, jobId)
    else safeSessionRemoveItem(STORAGE_KEYS.restoreStageJobId)
  }

  // A staging job was accepted. Its id is recorded first: should this section
  // be gone by now (a tab switch while the upload finished), the next mount
  // picks the job up from there.
  function startRestoreJob(jobId: string, snapshotId: string | null, epoch: number) {
    // Signed out while the upload / request was being answered: the staging job
    // is the signed-out admin's, not the next account's to be attached to.
    if (!adminSessionIsCurrent(epoch)) return
    rememberRestoreJob(jobId)
    if (!mountedRef.current) return
    setRestoreJob(placeholderJob(jobId, 'restore_stage', { snapshot_id: snapshotId }))
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      // An upload still sending its body is abandoned (nothing was staged).
      // A staged archive is kept: its job id is remembered, so the next
      // mount shows it again.
      uploadCtrlRef.current?.abort()
    }
  }, [])

  async function onRestoreFromId(item: BackupItem) {
    setRestoreError(null)
    const prior = holdPriorStage()
    setStagingFromId(item.id)
    const epoch = currentAdminSessionEpoch()
    try {
      const r = await adminRestoreFromSnapshot(item.id, { replaces: prior.stageId })
      startRestoreJob(r.job_id, item.id, epoch)
    } catch (err) {
      prior.restore()
      setRestoreError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    } finally {
      setStagingFromId(null)
    }
  }

  async function onUploadChange(ev: React.ChangeEvent<HTMLInputElement>) {
    const file = ev.target.files?.[0]
    ev.target.value = ''
    if (!file) return
    setRestoreError(null)
    const prior = holdPriorStage()
    setUploadProgress(0)
    setUploadStaging(false)
    const ctrl = new AbortController()
    uploadCtrlRef.current = ctrl
    let bodySent = false
    const epoch = currentAdminSessionEpoch()
    try {
      const r = await adminRestoreFromUpload(file, (p) => {
        if (mountedRef.current) setUploadProgress(p)
      }, {
        signal: ctrl.signal,
        replaces: prior.stageId,
        // The upload's own "body sent" event, not a progress figure (one that
        // rounds up to 100 would drop Cancel with the last MB still unsent).
        onSent: () => {
          bodySent = true
          // Once the whole body is sent the server stages it regardless, so
          // an abort from here on would only orphan that stage: stop offering
          // Cancel / aborting on unmount and let the response (and its job
          // id) arrive, recorded for the next mount if the section is gone.
          if (uploadCtrlRef.current === ctrl) uploadCtrlRef.current = null
          if (!mountedRef.current) return
          // Once the bytes have left the browser the server still has to
          // finish receiving them before it answers (then copies them into
          // staging in the job). Flip the UI to a "staging" indicator so the
          // operator doesn't see a stuck "uploading 100%".
          setUploadStaging(true)
        },
      })
      startRestoreJob(r.job_id, null, epoch)
    } catch (err) {
      // Refused, or never sent: nothing new was staged, so the archive staged
      // before is still the one. A lost answer after the whole body was sent
      // (no HTTP answer, or the proxy's own) may be an accepted job that has
      // removed it already: not offered again then.
      const unknown = bodySent && (isNetworkError(err)
        || (isApiCodeError(err) && err.code === 'adminDash.apiErr.restoreUploadGateway'))
      if (!unknown) prior.restore()
      if (isAbortError(err) || !mountedRef.current) return
      setRestoreError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    } finally {
      if (uploadCtrlRef.current === ctrl) uploadCtrlRef.current = null
      if (mountedRef.current) {
        setUploadProgress(null)
        setUploadStaging(false)
      }
    }
  }

  function onCancelUpload() {
    uploadCtrlRef.current?.abort()
  }

  async function onCancelStaged() {
    const stageId = stagedRestore?.stage_id
    setStagedRestore(null)
    rememberRestoreJob(null)
    setPreflight(null)
    setPreflightError(null)
    // Best-effort cleanup of the server-side stage so an aborted full-scope
    // restore doesn't pin the staging volume until the 24h sweep. The UI
    // closes immediately regardless — the call is fire-and-forget from the
    // operator's perspective.
    if (!stageId) return
    try {
      await adminCancelRestoreStage(stageId)
    } catch {
      // Silent: the stage will be reaped by sweep_stale_staging within 24h.
    }
  }

  // Pull the preflight summary as soon as a stage is available. The result
  // populates the "what's about to be deleted / installed" panel that gates
  // the destructive confirm dialog.
  useEffect(() => {
    if (!stagedRestore) {
      setPreflight(null)
      setPreflightError(null)
      return
    }
    const ctrl = new AbortController()
    // Drop the previous stage's result up front so it can't gate (or be
    // shown for) the new stage while this one loads.
    setPreflight(null)
    setPreflightLoading(true)
    setPreflightError(null)
    adminGetRestorePreflight(stagedRestore.stage_id, { signal: ctrl.signal })
      .then((p) => setPreflight(p))
      .catch((err) => {
        if (isAbortError(err)) return
        setPreflightError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
      })
      // An aborted (superseded) request must not clear the loading flag of
      // the request that replaced it.
      .finally(() => { if (!ctrl.signal.aborted) setPreflightLoading(false) })
    return () => ctrl.abort()
  }, [stagedRestore?.stage_id])

  async function onOpenResetPanel() {
    setResetError(null)
    setResetPreflightError(null)
    // Not the last opening's figures while this one's load is out.
    setResetPreflight(null)
    setResetPanelOpen(true)
    setResetPreflightLoading(true)
    try {
      const p = await adminGetResetPreflight()
      setResetPreflight(p)
    } catch (err) {
      setResetPreflightError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    } finally {
      setResetPreflightLoading(false)
    }
  }

  function onCancelResetPanel() {
    setResetPanelOpen(false)
    setResetPreflight(null)
    setResetPreflightError(null)
  }

  function onOpenResetConfirm() {
    setResetError(null)
    setResetConfirmText('')
    setResetPassword('')
    setResetConfirmOpen(true)
  }

  function onCancelResetConfirm() {
    setResetConfirmOpen(false)
    setResetConfirmText('')
    setResetPassword('')
  }

  // A successful restore / reset replaced the database, so everything the
  // dashboard holds in memory — slide list, viewer settings, metadata
  // options, the saved-snapshot baselines — now describes the pre-restore
  // state, and saving any of it (the viewer-settings Save sends every field)
  // would write that state back. Reload the whole page rather than refreshing
  // each piece; the carried key reopens this tab with the outcome banner.
  function reloadAfterRestore(result: unknown) {
    safeSessionSetItem(STORAGE_KEYS.adminPostRestore, JSON.stringify(result ?? null))
    window.location.reload()
  }

  // The service no longer accepts this tab's token (an apply signs everyone
  // out; restored accounts; or the token expired during the wait), so the
  // outcome can't be read here. Send the operator to log in, after an alert
  // saying why (`messageKey`); the carried key reopens this tab, whose mount
  // fetch shows the server's result banner — flagged when it is not this
  // apply's (stage_id mismatch).
  function reloginAfterRestore(stageId: string, messageKey: string) {
    window.alert(t(messageKey))
    safeSessionSetItem(STORAGE_KEYS.adminPostRestore, JSON.stringify({ awaitingStageId: stageId }))
    clearToken()
    window.location.assign('/admin')
  }

  // The alert for a waitForRestoreApplied rejection: it only says the service
  // restarted when an outage was actually observed; otherwise the wait simply
  // ran out while every poll was refused, and nothing is known about the
  // restore.
  function restartReloginKey(err: unknown): string {
    const sawOutage = err instanceof RestoreWaitUnauthorizedError ? err.sawOutage : true
    return sawOutage ? 'adminBackup.restore.backNeedsLogin' : 'adminBackup.restore.unconfirmedNeedsLogin'
  }

  async function onResetConfirmed() {
    setResetApplying(true)
    setResetError(null)
    try {
      let started: Awaited<ReturnType<typeof adminApplyReset>>
      try {
        started = await adminApplyReset(resetPassword)
      } catch (err) {
        // No answer: the reset may have started, and a retry could queue a
        // second one behind it.
        if (isNetworkError(err)) { setResetError(t('adminBackup.reset.outcomeUnknown')); return }
        throw err
      }
      // The job builds the reset stage first (minutes for a large database).
      setResetPreparing(true)
      let job: Awaited<ReturnType<typeof waitForJob>> | null = null
      let seenRunning = false
      let signedOut = false
      try {
        // keepLogin: applying the reset signs everyone out, this tab included
        // - the dev in-process apply before its job even reads as succeeded.
        // A 401 is then the expected end of the wait, not a cue to drop the
        // token and leave for the login screen with nothing to show for it.
        job = await waitForJob(started.job_id, {
          keepLogin: true,
          onPoll: (j) => { if (j.state === 'running') seenRunning = true },
        })
      } catch (err) {
        // Otherwise the job could not be followed to its end: the restart
        // that applies the reset (and signs everyone out) may already be
        // under way. The restore result below is matched by the stage id
        // instead.
        signedOut = isUnauthorizedError(err)
      } finally {
        setResetPreparing(false)
      }
      if (signedOut) {
        // Signed out after the job was seen at work: the reset was applied.
        // The result is read after signing in again, matched by stage id.
        reloginAfterRestore(
          started.stage_id,
          seenRunning ? 'adminBackup.reset.appliedNeedsLogin' : 'adminBackup.restore.unconfirmedNeedsLogin',
        )
        return
      }
      if (job?.state === 'failed') {
        // Translated when coded (a reset job whose worker exited).
        setResetError(restoreJobErrorText(t, job))
        return
      }
      if (job?.result?.mode === 'inprocess') {
        // In-process dev path, still signed in (signing everyone out failed;
        // the result file says so): the reset is already applied.
        reloadAfterRestore({
          ok: true,
          scope: 'reset',
          applied_at: new Date().toISOString(),
          app_version: currentVersion,
        })
        return
      }
      setResetWaitingHealth(true)
      let result: Awaited<ReturnType<typeof waitForRestoreApplied>>
      try {
        result = await waitForRestoreApplied(started.stage_id, 90_000)
      } catch (err) {
        if (isUnauthorizedError(err)) { reloginAfterRestore(started.stage_id, restartReloginKey(err)); return }
        // resetError is rendered ONLY inside the confirm modal, so this must
        // not fall through to the modal-closing below — otherwise the
        // failure is invisible and the reset looks like it succeeded.
        setResetError(t('adminBackup.reset.afterRestart'))
        return
      } finally {
        setResetWaitingHealth(false)
      }
      if (result.ok) { reloadAfterRestore(result); return }
      setLastResult(result)
      setResetConfirmOpen(false)
      setResetPanelOpen(false)
      setResetPassword('')
      setResetConfirmText('')
      // A reset parks the wiped live state under .pre-restore-* too.
      await refreshRollbacks()
    } catch (err) {
      setResetError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    } finally {
      setResetApplying(false)
    }
  }

  async function onApplyConfirmed() {
    if (!stagedRestore) return
    setApplying(true)
    setRestoreError(null)
    try {
      const stageId = stagedRestore.stage_id
      let r: Awaited<ReturnType<typeof adminApplyRestore>>
      // A transport failure is ambiguous (the server may already be restarting
      // into the apply), so it is followed by the same wait for the result
      // file as an accepted apply rather than reported as a plain failure: the
      // operator would otherwise retry an apply that is in flight.
      let ambiguous = false
      try {
        r = await adminApplyRestore(stageId, password)
      } catch (err) {
        // A definite rejection (wrong password, 4xx/5xx) means the server did
        // not start applying: the stage stays staged.
        if (!isNetworkError(err)) throw err
        ambiguous = true
        r = { mode: 'restart' } as Awaited<ReturnType<typeof adminApplyRestore>>
      }
      // The apply consumes the stage (or may be doing so): a remount must not
      // offer it again.
      rememberRestoreJob(null)
      if (r.mode === 'restart') {
        // The server is about to restart itself. Poll the result file until
        // it reports *this* apply (matched by stage_id) so the resolve
        // doesn't happen against the pre-restart uvicorn, which would otherwise
        // return a stale (or null) result and silently confuse the operator.
        setWaitingHealth(true)
        let result: Awaited<ReturnType<typeof waitForRestoreApplied>>
        try {
          result = await waitForRestoreApplied(stageId, 90_000)
        } catch (err) {
          if (isUnauthorizedError(err)) { reloginAfterRestore(stageId, restartReloginKey(err)); return }
          // The wait timed out (the restart is taking longer than 90s, or
          // failed). Keep the modal open and the staged restore intact so the
          // operator can see the error and retry/inspect — falling through to
          // the setStagedRestore(null)/setConfirmOpen(false) below would close
          // the only error surface and discard the stage_id while the restore
          // may still be applying.
          setRestoreError(t(ambiguous ? 'adminBackup.restore.outcomeUnknown' : 'adminBackup.restore.failedAfterRestart'))
          return
        } finally {
          setWaitingHealth(false)
        }
        if (result.ok) { reloadAfterRestore(result); return }
        // A failed swap was rolled back, so the in-memory state is still
        // current: show the error banner in place.
        setLastResult(result)
      } else {
        // In-process apply (dev) — ack immediately. Carry the scope so the
        // post-restore file-status effect fires for metadata restores (the
        // restart path gets scope from waitForRestoreApplied; without it here
        // the dev path would skip the WSI file-status check the reset path
        // already triggers).
        reloadAfterRestore({
          ok: true,
          applied_at: new Date().toISOString(),
          app_version: currentVersion,
          scope: stagedRestore.manifest.scope,
        })
        return
      }
      setStagedRestore(null)
      setConfirmOpen(false)
      setPassword('')
      await refreshList()
      // The apply just parked a new .pre-restore-* tree; surface it so the
      // operator can reclaim the (potentially WSI-sized) disk it now holds.
      await refreshRollbacks()
    } catch (err) {
      setRestoreError(restoreApplyErrorText(t, err))
    } finally {
      setApplying(false)
    }
  }

  // A failed / expired apply leaves its staged archive on disk (until the
  // 24 h sweep), and after a reload this tab no longer has it as the staged
  // restore, so nothing else offers to remove it.
  const [discardedStage, setDiscardedStage] = useState<string | null>(null)
  const [discardError, setDiscardError] = useState<string | null>(null)
  // One discard at a time: a second click would report the stage it just
  // removed as missing.
  const discardRequest = useInFlight()
  async function onDiscardResultStage(stageId: string) {
    await discardRequest.run(async () => {
      setDiscardError(null)
      try {
        await adminCancelRestoreStage(stageId)
        setDiscardedStage(stageId)
      } catch (err) {
        setDiscardError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
      }
    })
  }

  async function onDismissResult() {
    try {
      await adminClearRestoreResult()
    } catch { /* keep going */ }
    setLastResult(null)
    setAwaitedResultMissing(false)
  }

  async function checkFileStatus() {
    setFileStatusLoading(true)
    setFileStatusError(null)
    try {
      const status = await adminGetSlideFileStatus()
      setFileStatus(status)
    } catch (err) {
      setFileStatusError(apiErrorMessage(err, t) || t('adminBackup.error.generic'))
    } finally {
      setFileStatusLoading(false)
    }
  }

  // After a successful metadata restore or reset, automatically check file
  // status so the operator can see immediately whether WSI files need to be
  // (re-)uploaded.
  useEffect(() => {
    if (lastResult?.ok && (lastResult?.scope === 'metadata' || lastResult?.scope === 'reset')) {
      checkFileStatus()
    }
  }, [lastResult])

  // Close the confirm modal on Esc, but only when not already mid-apply (the
  // server is restarting at that point and dismissing the modal would lose
  // the only place waitingHealth / restoreError feedback is surfaced).
  useEffect(() => {
    if (!confirmOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !applying) {
        setConfirmOpen(false)
        setPassword('')
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [confirmOpen, applying])

  useEffect(() => {
    if (!confirmBackupOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setConfirmBackupOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [confirmBackupOpen])

  // Close the reset confirm modal on Esc, but not mid-apply (same rationale as
  // the restore modal above — the server is restarting and the modal is the
  // only place the reset feedback is surfaced). onCancelResetConfirm also
  // clears the typed confirmation text + password.
  useEffect(() => {
    if (!resetConfirmOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !resetApplying) onCancelResetConfirm()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [resetConfirmOpen, resetApplying])

  // Focus moves into each confirm modal while it is open (to its
  // data-autofocus control) and back to its trigger when it closes.
  const backupDialogRef = useRef<HTMLDivElement | null>(null)
  const restoreDialogRef = useRef<HTMLDivElement | null>(null)
  const resetDialogRef = useRef<HTMLDivElement | null>(null)
  useDialogFocus(backupDialogRef, confirmBackupOpen)
  useDialogFocus(restoreDialogRef, confirmOpen && !!stagedRestore)
  useDialogFocus(resetDialogRef, resetConfirmOpen)

  // ---- render ----
  const inProgress = backupJob && (backupJob.state === 'pending' || backupJob.state === 'running')
  const cancelling = !!inProgress && (cancelAsked || !!backupJob?.cancel_requested)
  const phaseKey = backupJob?.progress?.phase || 'queued'
  const phaseLabel = t(`adminBackup.phase.${phaseKey}`)
  // How far the WSI phase is, when the server reports it.
  const wsiRead = backupJob?.progress?.wsi_bytes_read
  const wsiTotal = backupJob?.progress?.wsi_bytes_total
  const wsiPercent = phaseKey === 'writing_wsi' && wsiRead != null && wsiTotal
    ? Math.min(100, Math.floor((wsiRead / wsiTotal) * 100))
    : null
  // Empty while the job runs, or when nothing was left out.
  const backupWarnings = backupWarningLines(t, backupJob?.result)
  const restoreStaging = !!restoreJob
  // Staging from a saved backup (that row's button shows it) or an upload.
  const restoreJobSnapshot = restoreJob?.progress?.snapshot_id ?? null
  const extracted = restoreJob?.progress?.bytes_extracted
  // The job's first step for an upload: copying it into staging.
  const copying = restoreJob?.progress?.phase === 'copying' ? restoreJob.progress : null
  const restoreBusy = uploadProgress !== null || stagingFromId !== null || restoreStaging
  const stagingLabel = copying
    ? `${t('adminBackup.restore.copyingOnServer')}${copying.total_bytes != null
      ? ` ${formatBytes(copying.bytes_copied ?? 0)} / ${formatBytes(copying.total_bytes)}` : ''}`
    : `${t('adminBackup.restore.stagingOnServer')}${extracted != null ? ` ${formatBytes(extracted)}` : ''}`

  return (
    <div style={SECTION}>
      <div>
        <h3 style={{ margin: 0, fontSize: 'var(--font-size-lg)', fontWeight: 700 }}>
          {t('adminBackup.title')}
        </h3>
        <p style={{ marginTop: 4, marginBottom: 0, color: 'var(--color-text-muted)', fontSize: 13 }}>
          {t('adminBackup.subtitle')}
        </p>
      </div>

      {viaTunnel && (
        <div style={WARN_STYLE}>{t('cloudflare.tunnel.warning')}</div>
      )}

      {awaitedResultMissing && (
        <div style={WARN_STYLE}>{t('adminBackup.restore.awaitedResultMissing')}</div>
      )}

      {lastResult && (
        <Card>
          <div style={CARD_PAD}>
            <div style={{ ...ROW, justifyContent: 'space-between' }}>
              <div>
                <strong>{t('adminBackup.restore.lastResultTitle')}</strong>{' '}
                {/* post_apply_error: the swap committed (ok stays true) but
                    the app could not be brought up to it afterwards; neither
                    a clean "OK" nor a "Failed" that suggests nothing moved. */}
                <Badge tone={!lastResult.ok ? 'danger' : lastResult.post_apply_error ? 'warning' : 'success'}>
                  {!lastResult.ok
                    ? t('adminBackup.restore.lastResult.failed')
                    : lastResult.post_apply_error
                      ? t('adminBackup.restore.lastResult.needsRestart')
                      : t('adminBackup.restore.lastResult.ok')}
                </Badge>
              </div>
              <Button size="sm" variant="ghost" onClick={onDismissResult}>{t('adminBackup.restore.dismiss')}</Button>
            </div>
            {lastResult.ok && lastResult.post_apply_error && (
              <div role="alert" style={{ ...ERROR_STYLE, marginTop: 8 }}>
                {t('adminBackup.restore.postApplyError', { error: String(lastResult.post_apply_error) })}
              </div>
            )}
            {lastResult.ok && !lastResult.post_apply_error && (
              <div style={{ marginTop: 8, fontSize: 14 }}>
                {lastResult.scope === 'reset' ? t('adminBackup.reset.complete') : t('adminBackup.restore.complete')}
              </div>
            )}
            {/* Not while a new staging is under way: the stage it replaces is
                held for it (holdPriorStage) and may be this one. Only for a
                real stage id ("?" marks an unreadable request). */}
            {!lastResult.ok && lastResult.stage_id?.startsWith('stage-')
              && lastResult.stage_id !== stagedRestore?.stage_id && !restoreBusy && (
              <div style={{ ...ROW, marginTop: 8, fontSize: 13 }}>
                {discardedStage === lastResult.stage_id ? (
                  <span>{t('adminBackup.restore.stageDiscarded')}</span>
                ) : (
                  <Button size="sm" variant="secondary" disabled={discardRequest.busy} onClick={() => onDiscardResultStage(lastResult.stage_id!)}>
                    {t('adminBackup.restore.discardStage')}
                  </Button>
                )}
                {discardError && <span style={ERROR_STYLE}>{discardError}</span>}
              </div>
            )}
            {lastResult.session_revocation_error && (
              <div role="alert" style={{ ...WARN_STYLE, marginTop: 8 }}>
                {t('adminBackup.restore.sessionRevocationError', { error: String(lastResult.session_revocation_error) })}
              </div>
            )}
            <pre style={{ marginTop: 8, fontSize: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
              {JSON.stringify(lastResult, null, 2)}
            </pre>
          </div>
        </Card>
      )}

      {/* CREATE */}
      <Card>
        <div style={CARD_PAD}>
          <h4 style={{ margin: 0, marginBottom: 'var(--space-3)' }}>{t('adminBackup.create')}</h4>

          <div style={{ marginBottom: 'var(--space-3)' }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
              {t('adminBackup.components')}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {/* WSI image files: maps to the metadata/full scope server-side. */}
              <ComponentToggle
                checked={includeWsi}
                onChange={() => setIncludeWsi((v) => !v)}
                disabled={!!inProgress || startingBackup}
                label={t('adminBackup.include.wsi')}
                help={t('adminBackup.include.wsi.help')}
                sizeBytes={estimate ? estimate.wsi + estimate.thumbs : null}
              />
              <ComponentToggle
                checked={includeAnalytics}
                onChange={() => setIncludeAnalytics((v) => !v)}
                disabled={!!inProgress || startingBackup}
                label={t('adminBackup.include.analytics')}
                help={t('adminBackup.include.analytics.help')}
                sizeBytes={estimate ? estimate.analytics : null}
              />
              <ComponentToggle
                checked={includeHistory}
                onChange={() => setIncludeHistory((v) => !v)}
                disabled={!!inProgress || startingBackup}
                label={t('adminBackup.include.history')}
                help={t('adminBackup.include.history.help')}
                sizeBytes={estimate ? estimate.history : null}
              />
            </div>
            {/* DB core (slides, tours, settings) is always captured. */}
            <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginTop: 8 }}>
              {t('adminBackup.dbCore')}
              {estimate && (
                <span style={{ marginLeft: 6, fontFamily: 'monospace' }}>
                  {formatBytes(estimate.db_core)}
                </span>
              )}
            </div>
            {estimate && (
              <div style={{ fontSize: 13, marginTop: 8, fontWeight: 600 }}>
                {t('adminBackup.estimate.total')}:{' '}
                <span style={{ fontFamily: 'monospace' }}>
                  {formatBytes(
                    estimate.db_core +
                      (includeWsi ? estimate.wsi + estimate.thumbs : 0) +
                      (includeAnalytics ? estimate.analytics : 0) +
                      (includeHistory ? estimate.history : 0),
                  )}
                </span>
                <span style={{ marginLeft: 6, fontWeight: 400, color: 'var(--color-text-muted)' }}>
                  {t('adminBackup.estimate.note')}
                </span>
              </div>
            )}
            <div style={{ ...WARN_STYLE, marginTop: 8, fontSize: 12 }}>
              {t('adminBackup.credentialsNote')}
            </div>
          </div>

          <div style={{ marginBottom: 'var(--space-3)' }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{t('adminBackup.note')}</div>
            <Textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={t('adminBackup.notePlaceholder')}
              rows={2}
              // Also while the start request is in flight: what it sends is
              // what was set at the click, so a later edit would show beside
              // a backup that doesn't carry it.
              disabled={!!inProgress || startingBackup}
            />
          </div>

          <Button variant="primary" onClick={onCreate} disabled={!!inProgress || startingBackup || verifyingId !== null}>
            {inProgress || startingBackup ? t('adminBackup.starting') : t('adminBackup.startBackup')}
          </Button>

          {inProgress && (
            <div style={{ marginTop: 'var(--space-3)' }}>
              <div style={{ ...ROW, justifyContent: 'space-between' }}>
                <div style={{ fontSize: 13 }}>
                  {cancelling ? t('adminBackup.cancel.cancelling') : `${t('adminBackup.inProgress')} — ${phaseLabel}`}
                  {backupJob?.progress?.bytes_written != null && (
                    <span style={{ marginLeft: 8, color: 'var(--color-text-muted)' }}>
                      {formatBytes(backupJob.progress.bytes_written)}
                    </span>
                  )}
                  {wsiPercent != null && wsiTotal != null && (
                    <span style={{ marginLeft: 8, color: 'var(--color-text-muted)' }}>
                      {t('adminBackup.wsiProgress', {
                        percent: wsiPercent,
                        read: formatBytes(wsiRead ?? 0),
                        total: formatBytes(wsiTotal),
                      })}
                    </span>
                  )}
                </div>
                {/* A placeholder job (id known, first poll not answered yet)
                    can be cancelled too: the server knows it. */}
                <Button size="sm" variant="ghost" onClick={onCancelBackup} disabled={cancelling}>
                  {cancelling ? t('adminBackup.cancel.cancellingButton') : t('adminBackup.cancel.button')}
                </Button>
              </div>
              {cancelError && <div style={ERROR_STYLE}>{cancelError}</div>}
            </div>
          )}

          {backupError && <div style={ERROR_STYLE}>{backupError}</div>}
          {otherJob && !inProgress && (
            <div role="status" style={{ marginTop: 'var(--space-3)', fontSize: 13, color: 'var(--color-text-muted)' }}>
              {t('adminBackup.otherJobRunning', { kind: t(`adminBackup.jobKind.${otherJob.kind}`) })}
            </div>
          )}
          {backupNotice && !inProgress && (
            <div role="status" style={{ marginTop: 'var(--space-3)', fontSize: 13, color: 'var(--color-text-muted)' }}>
              {backupNotice}
            </div>
          )}
          {/* What the published archive left out (a tour attachment removed
              mid-run, a thumbnail that changed after the snapshot): the
              backup succeeded, but a restore will lack these. */}
          {backupJob?.state === 'succeeded' && backupWarnings.length > 0 && (
            <div style={{ ...WARN_STYLE, marginTop: 'var(--space-3)' }}>
              <div>{t('adminBackup.createdWithWarnings')}</div>
              <ul style={{ margin: 'var(--space-1) 0 0', paddingLeft: 20 }}>
                {backupWarnings.map((w, i) => <li key={i}>{w}</li>)}
              </ul>
            </div>
          )}
        </div>
      </Card>

      {/* LIST */}
      <Card>
        <div style={CARD_PAD}>
          <h4 style={{ margin: 0, marginBottom: 'var(--space-3)' }}>{t('adminBackup.list.title')}</h4>
          {listError && <div style={ERROR_STYLE}>{listError}</div>}
          {verifyOutcome && (
            <div role={verifyOutcome.ok ? 'status' : 'alert'} style={verifyOutcome.ok ? { fontSize: 13, marginTop: 4 } : ERROR_STYLE}>
              {verifyOutcome.text}
            </div>
          )}
          {!list ? (
            <div style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>—</div>
          ) : list.items.length === 0 ? (
            <div style={{ color: 'var(--color-text-muted)', fontSize: 13 }}>
              {t('adminBackup.list.empty')}
            </div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table style={TABLE_STYLE}>
                <thead>
                  <tr>
                    <th style={TH_STYLE}>{t('adminBackup.col.created')}</th>
                    <th style={TH_STYLE}>{t('adminBackup.col.scope')}</th>
                    <th style={TH_STYLE}>{t('adminBackup.col.size')}</th>
                    <th style={TH_STYLE}>{t('adminBackup.col.appVersion', { current: currentVersion })}</th>
                    <th style={TH_STYLE}>{t('adminBackup.col.note')}</th>
                    <th style={TH_STYLE}>{t('adminBackup.col.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {list.items.map((item) => {
                    return (
                      <tr key={item.id}>
                        <td style={TD_STYLE}>
                          <div>{formatDateTime(item.created_at)}</div>
                          <div style={{ fontSize: 11, color: 'var(--color-text-faint)', fontFamily: 'monospace' }}>
                            {item.id}
                          </div>
                          {item.sha256 && (
                            <div style={{ fontSize: 10, color: 'var(--color-text-faint)', fontFamily: 'monospace' }}>
                              sha256:{shortSha(item.sha256)}
                            </div>
                          )}
                        </td>
                        <td style={TD_STYLE}>
                          {/* Show every selectable component with an included /
                              excluded marker, so the type is read directly off
                              the row instead of being inferred from the
                              metadata/full label plus only-when-missing badges.
                              WSI presence is carried by scope (full vs metadata);
                              the other two come from their own flags. */}
                          {(() => {
                            // Short cell labels keep the column narrow inside the
                            // width-limited sidebar; the full component name +
                            // included / excluded state lives in the hover title.
                            const mark = (on: boolean) =>
                              on ? t('adminBackup.component.included') : t('adminBackup.component.excluded')
                            const rows: { included: boolean; label: string; full: string }[] = [
                              {
                                included: item.scope !== 'metadata',
                                label: t('adminBackup.component.wsi'),
                                full: t('adminBackup.include.wsi'),
                              },
                              {
                                included: item.includes_analytics !== false,
                                label: t('adminBackup.component.analytics'),
                                full: t('adminBackup.include.analytics'),
                              },
                              {
                                included: item.includes_history !== false,
                                label: t('adminBackup.component.history'),
                                full: t('adminBackup.include.history'),
                              },
                            ]
                            return (
                              <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                                {rows.map((r) => (
                                  <ComponentStatus
                                    key={r.label}
                                    included={r.included}
                                    label={r.label}
                                    title={`${r.full}: ${mark(r.included)}`}
                                  />
                                ))}
                              </div>
                            )
                          })()}
                        </td>
                        <td style={TD_STYLE}>{formatBytes(item.size_bytes)}</td>
                        <td style={TD_STYLE}>
                          <span style={{ fontFamily: 'monospace' }}>{item.app_version || '?'}</span>
                        </td>
                        <td style={TD_STYLE}>{item.note || '—'}</td>
                        <td style={TD_STYLE}>
                          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                            <Button size="sm" variant="secondary" onClick={() => onDownload(item)}>
                              {t('adminBackup.action.download')}
                            </Button>
                            <Button
                              size="sm"
                              variant="secondary"
                              onClick={() => onRestoreFromId(item)}
                              // Mutually exclusive with an in-flight upload-restore,
                              // another row's staging and a running staging job: they
                              // all write the single stagedRestore slot, so letting
                              // them overlap would leave the shown preview/stage_id
                              // out of sync with what was actually staged on the
                              // server. The upload button uses the same guard.
                              disabled={restoreBusy}
                            >
                              {stagingFromId === item.id || (restoreStaging && restoreJobSnapshot === item.id)
                                ? t('adminBackup.restore.staging')
                                : t('adminBackup.action.restore')}
                            </Button>
                            {verifyingId === item.id ? (
                              <Button size="sm" variant="ghost" onClick={onCancelVerify} disabled={!verifyJob || cancelVerifyRequest.busy || verifyStopAskedFor === verifyJob.id}>
                                {t('adminBackup.verify.cancel')}
                              </Button>
                            ) : (
                              <Button
                                size="sm"
                                variant="secondary"
                                onClick={() => onVerify(item)}
                                // One at a time: it takes the job slot.
                                disabled={verifyingId !== null || !!inProgress || restoreBusy || startingBackup}
                              >
                                {t('adminBackup.action.verify')}
                              </Button>
                            )}
                            <Button
                              size="sm"
                              variant="danger"
                              onClick={() => onDelete(item)}
                              // The restore being staged from this row reads it
                              // (the server refuses the delete with a 409 too).
                              disabled={deleteRequest.busy || stagingFromId === item.id || (restoreStaging && restoreJobSnapshot === item.id) || verifyingId === item.id}
                            >
                              {t('adminBackup.action.delete')}
                            </Button>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </Card>

      {/* PRE-RESTORE ROLLBACK DIRECTORIES (reclaim disk) */}
      {rollbacks && rollbacks.items.length > 0 && (
        <Card>
          <div style={CARD_PAD}>
            <h4 style={{ margin: 0, marginBottom: 'var(--space-2)' }}>
              {t('adminBackup.rollback.title')}
            </h4>
            <p style={{ marginTop: 0, marginBottom: 'var(--space-3)', color: 'var(--color-text-muted)', fontSize: 13 }}>
              {t('adminBackup.rollback.subtitle')}
            </p>
            {rollbacksError && <div style={ERROR_STYLE}>{rollbacksError}</div>}
            <div style={{ overflowX: 'auto' }}>
              <table style={TABLE_STYLE}>
                <thead>
                  <tr>
                    <th style={TH_STYLE}>{t('adminBackup.col.created')}</th>
                    <th style={TH_STYLE}>{t('adminBackup.col.size')}</th>
                    <th style={TH_STYLE}>{t('adminBackup.col.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {rollbacks.items.map((rb) => (
                    <tr key={rb.name}>
                      <td style={TD_STYLE}>
                        <div>{rb.created_at ? formatDateTime(rb.created_at) : '—'}</div>
                        <div style={{ fontSize: 11, color: 'var(--color-text-faint)', fontFamily: 'monospace' }}>
                          {rb.name}
                        </div>
                      </td>
                      <td style={TD_STYLE}>{formatBytes(rb.size_bytes)}</td>
                      <td style={TD_STYLE}>
                        <Button
                          size="sm"
                          variant="danger"
                          disabled={deletingRollback !== null}
                          onClick={() => onDeleteRollback(rb.name)}
                        >
                          {deletingRollback === rb.name
                            ? t('adminBackup.rollback.deleting')
                            : t('adminBackup.action.delete')}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </Card>
      )}

      {/* RESTORE FROM UPLOAD */}
      <Card>
        <div style={CARD_PAD}>
          <h4 style={{ margin: 0, marginBottom: 'var(--space-2)' }}>{t('adminBackup.restore.title')}</h4>
          <p style={{ marginTop: 0, marginBottom: 'var(--space-3)', color: 'var(--color-text-muted)', fontSize: 13 }}>
            {t('adminBackup.restore.subtitle')}
          </p>

          <div style={ROW}>
            <input
              ref={fileInputRef}
              type="file"
              accept=".tar.zst,.tar.gz,.tgz,application/zstd,application/gzip"
              style={{ display: 'none' }}
              onChange={onUploadChange}
            />
            <Button
              variant="secondary"
              onClick={() => fileInputRef.current?.click()}
              disabled={restoreBusy}
            >
              {uploadStaging || (restoreStaging && !restoreJobSnapshot)
                ? stagingLabel
                : uploadProgress !== null
                ? `${t('adminBackup.restore.uploading')} ${uploadProgress}%`
                : t('adminBackup.restore.fromUpload')}
            </Button>
            {uploadProgress !== null && !uploadStaging && (
              <Button variant="ghost" onClick={onCancelUpload}>
                {t('adminBackup.restore.cancel')}
              </Button>
            )}
            <span style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>
              {t('adminBackup.restore.uploadHint')}
            </span>
          </div>

          {restoreError && <div style={ERROR_STYLE}>{restoreError}</div>}
        </div>
      </Card>

      {/* STAGED PREVIEW */}
      {stagedRestore && (
        <Card>
          <div style={CARD_PAD}>
            <h4 style={{ margin: 0, marginBottom: 'var(--space-3)' }}>
              {t('adminBackup.restore.preview.title')}
            </h4>
            <table style={TABLE_STYLE}>
              <tbody>
                <tr>
                  <td style={TD_STYLE}>{t('adminBackup.restore.preview.from')}</td>
                  <td style={{ ...TD_STYLE, fontFamily: 'monospace' }}>
                    {stagedRestore.plan.backup_app_version}
                  </td>
                </tr>
                <tr>
                  <td style={TD_STYLE}>{t('adminBackup.restore.preview.to')}</td>
                  <td style={{ ...TD_STYLE, fontFamily: 'monospace' }}>
                    {stagedRestore.plan.current_app_version}
                  </td>
                </tr>
                <tr>
                  <td style={TD_STYLE}>{t('adminBackup.restore.preview.scope')}</td>
                  <td style={TD_STYLE}>{stagedRestore.manifest.scope}</td>
                </tr>
                <tr>
                  <td style={TD_STYLE}>{t('adminBackup.restore.preview.note')}</td>
                  <td style={TD_STYLE}>{stagedRestore.manifest.note || '—'}</td>
                </tr>
                <tr>
                  <td style={TD_STYLE}>{t('adminBackup.restore.preview.slides')}</td>
                  <td style={TD_STYLE}>{stagedRestore.manifest.slide_count}</td>
                </tr>
                <tr>
                  <td style={TD_STYLE}>{t('adminBackup.restore.preview.tours')}</td>
                  <td style={TD_STYLE}>{stagedRestore.manifest.tour_count}</td>
                </tr>
              </tbody>
            </table>

            {stagedRestore.plan.blocking_reason && (
              <div style={ERROR_STYLE}>
                <strong>{t('adminBackup.restore.blocked')}:</strong> {restoreBlockingText(t, stagedRestore.plan)}
              </div>
            )}

            {/* Preflight summary: "what will be deleted / installed". The
                operator needs this before confirming so a metadata-scope
                restore doesn't surprise them by wiping wsi/. */}
            <div style={{ marginTop: 'var(--space-3)' }}>
              <strong>{t('adminBackup.preflight.title')}</strong>
              {preflightLoading && (
                <div style={{ marginTop: 4, fontSize: 13, color: 'var(--color-text-muted)' }}>
                  {t('adminBackup.preflight.loading')}
                </div>
              )}
              {preflightError && (
                <div style={ERROR_STYLE}>
                  {t('adminBackup.preflight.error', { error: preflightError })}
                </div>
              )}
              {preflight && (
                <div style={{ marginTop: 4, fontSize: 13 }}>
                  <div style={{ display: 'flex', gap: 'var(--space-4)', flexWrap: 'wrap' }}>
                    <div>
                      <div style={{ fontWeight: 600, marginBottom: 2 }}>
                        {t('adminBackup.preflight.willDelete')}
                      </div>
                      <div>{t('adminBackup.preflight.slides', { count: preflight.live_state.slide_count })}</div>
                      <div>{t('adminBackup.preflight.tours', { count: preflight.live_state.tour_count })}</div>
                      <div>{t('adminBackup.preflight.wsiFiles', {
                        count: preflight.live_state.wsi_file_count,
                        bytes: formatBytes(preflight.live_state.wsi_total_bytes),
                      })}</div>
                      <div>{t('adminBackup.preflight.assetsBytes', {
                        bytes: formatBytes(preflight.live_state.assets_total_bytes),
                      })}</div>
                    </div>
                    <div>
                      <div style={{ fontWeight: 600, marginBottom: 2 }}>
                        {t('adminBackup.preflight.willInstall')}
                      </div>
                      <div>{t('adminBackup.preflight.slides', { count: preflight.archive_state.slide_count })}</div>
                      <div>{t('adminBackup.preflight.tours', { count: preflight.archive_state.tour_count })}</div>
                      <div>{t('adminBackup.preflight.wsiFiles', {
                        count: preflight.archive_state.wsi_file_count,
                        bytes: formatBytes(preflight.archive_state.wsi_total_bytes),
                      })}</div>
                    </div>
                  </div>
                  <div style={{ marginTop: 6 }}>
                    {preflight.archive_state.wsi_uploads_pending > 0 ? (
                      <div style={WARN_STYLE}>
                        {t('adminBackup.preflight.uploadsPending', {
                          count: preflight.archive_state.wsi_uploads_pending,
                        })}
                      </div>
                    ) : (
                      <div style={{ color: 'var(--color-text-muted)' }}>
                        {t('adminBackup.preflight.uploadsNone')}
                      </div>
                    )}
                  </div>
                  <div style={{ marginTop: 6 }}>
                    {preflight.integrity.ok ? (
                      <span style={{ color: 'var(--color-success, #1a7f37)' }}>
                        {t('adminBackup.preflight.integrity.ok')}
                      </span>
                    ) : (
                      <div style={ERROR_STYLE}>
                        <strong>{t('adminBackup.preflight.integrity.failed')}</strong>
                        {preflight.integrity.missing_wsi_files.length > 0 && (
                          <div>{t('adminBackup.preflight.integrity.missingWsi', {
                            count: preflight.integrity.missing_wsi_files.length,
                          })}</div>
                        )}
                        {preflight.integrity.unexpected_wsi_files.length > 0 && (
                          <div>{t('adminBackup.preflight.integrity.unexpectedWsi', {
                            count: preflight.integrity.unexpected_wsi_files.length,
                          })}</div>
                        )}
                        {preflight.integrity.errors.map((e, i) => (
                          <div key={i} style={{ fontSize: 12 }}>{e}</div>
                        ))}
                      </div>
                    )}
                    {(preflight.integrity.warnings ?? []).length > 0 && (
                      <div style={{ ...WARN_STYLE, marginTop: 6 }}>
                        <strong>{t('adminBackup.preflight.integrity.warnings')}</strong>
                        {preflight.integrity.warnings.map((w, i) => (
                          <div key={i} style={{ fontSize: 12 }}>{w}</div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>

            <div style={{ ...ROW, marginTop: 'var(--space-3)' }}>
              <Button variant="ghost" onClick={onCancelStaged}>
                {t('adminBackup.restore.cancel')}
              </Button>
              <Button
                variant="danger"
                onClick={() => setConfirmOpen(true)}
                disabled={
                  !!stagedRestore.plan.blocking_reason
                  || preflightLoading
                  // Require a successful preflight for THIS stage: a failed
                  // or not-yet-loaded one leaves the operator confirming
                  // without seeing what the restore deletes / installs.
                  || !preflight
                  || preflight.stage_id !== stagedRestore.stage_id
                  || !preflight.integrity.ok
                }
              >
                {t('adminBackup.restore.apply')}
              </Button>
            </div>
          </div>
        </Card>
      )}

      {/* RESET (danger zone) */}
      <Card>
        <div style={CARD_PAD}>
          <h4 style={{ margin: 0, marginBottom: 'var(--space-2)' }}>
            {t('adminBackup.reset.title')}
          </h4>
          <p style={{ marginTop: 0, marginBottom: 'var(--space-3)', color: 'var(--color-text-muted)', fontSize: 13 }}>
            {t('adminBackup.reset.subtitle')}
          </p>

          {!resetPanelOpen ? (
            <Button variant="danger" onClick={onOpenResetPanel}>
              {t('adminBackup.reset.button')}
            </Button>
          ) : (
            <div>
              {resetPreflightLoading && (
                <div style={{ fontSize: 13, color: 'var(--color-text-muted)' }}>
                  {t('adminBackup.reset.preflightLoading')}
                </div>
              )}
              {resetPreflightError && (
                <div style={ERROR_STYLE}>
                  {t('adminBackup.reset.preflightError', { error: resetPreflightError })}
                </div>
              )}
              {resetPreflight && (
                <div>
                  <div style={WARN_STYLE}>
                    {t('adminBackup.reset.warning')}
                  </div>
                  <div style={{ display: 'flex', gap: 'var(--space-4)', flexWrap: 'wrap', marginTop: 'var(--space-3)', fontSize: 13 }}>
                    <div>
                      <div style={{ fontWeight: 600, marginBottom: 2 }}>
                        {t('adminBackup.reset.willDelete')}
                      </div>
                      <div>{t('adminBackup.preflight.slides', { count: resetPreflight.live_state.slide_count })}</div>
                      <div>{t('adminBackup.preflight.tours', { count: resetPreflight.live_state.tour_count })}</div>
                      <div>{t('adminBackup.preflight.wsiFiles', {
                        count: resetPreflight.live_state.wsi_file_count,
                        bytes: formatBytes(resetPreflight.live_state.wsi_total_bytes),
                      })}</div>
                      <div>{t('adminBackup.preflight.assetsBytes', {
                        bytes: formatBytes(resetPreflight.live_state.assets_total_bytes),
                      })}</div>
                    </div>
                    <div>
                      <div style={{ fontWeight: 600, marginBottom: 2 }}>
                        {t('adminBackup.reset.willKeep')}
                      </div>
                      <div>{t('adminBackup.reset.kept.admins')}</div>
                      <div>{t('adminBackup.reset.kept.settings')}</div>
                    </div>
                  </div>
                  <div style={{ ...ROW, marginTop: 'var(--space-3)' }}>
                    <Button variant="ghost" onClick={onCancelResetPanel}>
                      {t('adminBackup.restore.cancel')}
                    </Button>
                    <Button variant="danger" onClick={onOpenResetConfirm}>
                      {t('adminBackup.reset.button')}
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </Card>

      {/* WSI FILE STATUS */}
      <Card>
        <div style={CARD_PAD}>
          <div style={{ ...ROW, justifyContent: 'space-between', marginBottom: 'var(--space-2)' }}>
            <h4 style={{ margin: 0 }}>{t('adminBackup.wsiFiles.title')}</h4>
            <Button
              size="sm"
              variant="secondary"
              onClick={checkFileStatus}
              disabled={fileStatusLoading}
            >
              {fileStatusLoading ? t('adminBackup.wsiFiles.checking') : t('adminBackup.wsiFiles.check')}
            </Button>
          </div>
          <p style={{ margin: '0 0 var(--space-3)', color: 'var(--color-text-muted)', fontSize: 13 }}>
            {t('adminBackup.wsiFiles.subtitle')}
          </p>
          {fileStatusError && <div style={ERROR_STYLE}>{fileStatusError}</div>}
          {fileStatus && (
            <div>
              {fileStatus.missing === 0 ? (
                <div style={{ color: 'var(--color-success, #1a7f37)', fontSize: 13, marginBottom: 'var(--space-2)' }}>
                  {t('adminBackup.wsiFiles.allPresent', { count: fileStatus.total })}
                </div>
              ) : (
                <div style={{ marginBottom: 'var(--space-3)' }}>
                  <div style={{ ...WARN_STYLE, marginBottom: 'var(--space-2)' }}>
                    {t('adminBackup.wsiFiles.missing', { missing: fileStatus.missing, total: fileStatus.total })}
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginBottom: 'var(--space-2)' }}>
                    {t('adminBackup.wsiFiles.copyHint', { wsi_dir: fileStatus.wsi_dir })}
                  </div>
                  <div style={{ overflowX: 'auto' }}>
                    <table style={TABLE_STYLE}>
                      <thead>
                        <tr>
                          <th style={TH_STYLE}>{t('adminBackup.wsiFiles.col.id')}</th>
                          <th style={TH_STYLE}>{t('adminBackup.wsiFiles.col.filename')}</th>
                          <th style={TH_STYLE}>{t('adminBackup.wsiFiles.col.title')}</th>
                          <th style={TH_STYLE}>{t('adminBackup.wsiFiles.col.status')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {fileStatus.slides.filter(s => !s.file_exists).map(s => (
                          <tr key={s.id}>
                            <td style={TD_STYLE}>{s.id}</td>
                            <td style={{ ...TD_STYLE, fontFamily: 'monospace', fontSize: 12 }}>{s.wsi_filename || '—'}</td>
                            <td style={TD_STYLE}>{s.title || s.catalog_id || '—'}</td>
                            <td style={{ ...TD_STYLE, color: 'var(--color-danger, #b00020)', fontWeight: 600, fontSize: 12 }}>
                              {t('adminBackup.wsiFiles.missingLabel')}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
              {fileStatus.orphaned_files.length > 0 && (
                <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginTop: 'var(--space-2)' }}>
                  {t('adminBackup.wsiFiles.orphaned', { count: fileStatus.orphaned_files.length })}
                </div>
              )}
              {(fileStatus.temp_files ?? []).length > 0 && (
                <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginTop: 'var(--space-2)' }}>
                  {t('adminBackup.wsiFiles.tempFiles', { count: fileStatus.temp_files.length })}
                </div>
              )}
            </div>
          )}
        </div>
      </Card>

      {/* BACKUP CONFIRMATION MODAL */}
      {confirmBackupOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="backup-create-title"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setConfirmBackupOpen(false)
          }}
          style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
          }}
        >
          <div ref={backupDialogRef} tabIndex={-1} style={{
            background: 'var(--color-surface)',
            padding: 'var(--space-4)',
            borderRadius: 8,
            maxWidth: 480,
            width: '90%',
            boxShadow: '0 4px 24px rgba(0,0,0,0.2)',
          }}>
            <h4 id="backup-create-title" style={{ marginTop: 0 }}>{t('adminBackup.confirmCreate.title')}</h4>
            <p style={{ fontSize: 14 }}>{t('adminBackup.confirmCreate.body')}</p>
            <div style={{ ...ROW, marginTop: 'var(--space-3)', justifyContent: 'flex-end' }}>
              <Button variant="ghost" onClick={() => setConfirmBackupOpen(false)}>
                {t('adminBackup.restore.cancel')}
              </Button>
              <Button variant="primary" onClick={startBackupNow} data-autofocus>
                {t('adminBackup.startBackup')}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* CONFIRMATION MODAL */}
      {confirmOpen && stagedRestore && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="backup-restore-title"
          onMouseDown={(e) => {
            // Backdrop click (target === currentTarget) closes; clicks that
            // bubble up from the inner panel are ignored. Disabled while a
            // restore is mid-apply so the user can't accidentally hide the
            // restart progress / error feedback.
            if (e.target === e.currentTarget && !applying) {
              setConfirmOpen(false)
              setPassword('')
            }
          }}
          style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
          }}
        >
          <div ref={restoreDialogRef} tabIndex={-1} style={{
            background: 'var(--color-surface)',
            padding: 'var(--space-4)',
            borderRadius: 8,
            maxWidth: 480,
            width: '90%',
            boxShadow: '0 4px 24px rgba(0,0,0,0.2)',
          }}>
            <h4 id="backup-restore-title" style={{ marginTop: 0 }}>{t('adminBackup.restore.confirm.title')}</h4>
            <p style={{ fontSize: 14 }}>{t('adminBackup.restore.confirm.body')}</p>
            <p style={{ fontSize: 14 }}>{t('adminBackup.restore.confirm.password')}</p>
            {/* Locked while the restore is out: its answer clears it. */}
            <LockFieldset locked={applying}>
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t('adminBackup.restore.passwordPlaceholder')}
                data-autofocus
              />
            </LockFieldset>
            {restoreError && <div style={ERROR_STYLE}>{restoreError}</div>}
            {waitingHealth && (
              <div style={{ marginTop: 8, fontSize: 13, color: 'var(--color-text-muted)' }}>
                {t('adminBackup.restore.waitingForRestart')}
              </div>
            )}
            <div style={{ ...ROW, marginTop: 'var(--space-3)', justifyContent: 'flex-end' }}>
              <Button
                variant="ghost"
                onClick={() => { setConfirmOpen(false); setPassword('') }}
                disabled={applying}
              >
                {t('adminBackup.restore.cancel')}
              </Button>
              <Button
                variant="danger"
                onClick={onApplyConfirmed}
                disabled={applying || !password}
              >
                {applying ? t('adminBackup.restore.applying') : t('adminBackup.restore.apply')}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* RESET CONFIRMATION MODAL */}
      {resetConfirmOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="backup-reset-title"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget && !resetApplying) onCancelResetConfirm()
          }}
          style={{
            position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
          }}
        >
          <div ref={resetDialogRef} tabIndex={-1} style={{
            background: 'var(--color-surface)',
            padding: 'var(--space-4)',
            borderRadius: 8,
            maxWidth: 480,
            width: '90%',
            boxShadow: '0 4px 24px rgba(0,0,0,0.2)',
          }}>
            <h4 id="backup-reset-title" style={{ marginTop: 0 }}>{t('adminBackup.reset.confirm.title')}</h4>
            <p style={{ fontSize: 14 }}>{t('adminBackup.reset.confirm.body')}</p>
            <div style={{ marginTop: 'var(--space-2)' }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                {t('adminBackup.reset.confirm.text')}
              </div>
              {/* Locked while the reset is out: its answer clears both. */}
              <LockFieldset locked={resetApplying}>
                <Input
                  value={resetConfirmText}
                  onChange={(e) => setResetConfirmText(e.target.value)}
                  placeholder={t('adminBackup.reset.confirm.textPlaceholder')}
                  data-autofocus
                />
              </LockFieldset>
            </div>
            <div style={{ marginTop: 'var(--space-2)' }}>
              <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 4 }}>
                {t('adminBackup.reset.confirm.password')}
              </div>
              <LockFieldset locked={resetApplying}>
                <Input
                  type="password"
                  value={resetPassword}
                  onChange={(e) => setResetPassword(e.target.value)}
                  placeholder={t('adminBackup.restore.passwordPlaceholder')}
                />
              </LockFieldset>
            </div>
            {resetError && <div style={ERROR_STYLE}>{resetError}</div>}
            {resetPreparing && (
              <div style={{ marginTop: 8, fontSize: 13, color: 'var(--color-text-muted)' }}>
                {t('adminBackup.reset.preparing')}
              </div>
            )}
            {resetWaitingHealth && (
              <div style={{ marginTop: 8, fontSize: 13, color: 'var(--color-text-muted)' }}>
                {t('adminBackup.restore.waitingForRestart')}
              </div>
            )}
            <div style={{ ...ROW, marginTop: 'var(--space-3)', justifyContent: 'flex-end' }}>
              <Button variant="ghost" onClick={onCancelResetConfirm} disabled={resetApplying}>
                {t('adminBackup.restore.cancel')}
              </Button>
              <Button
                variant="danger"
                onClick={onResetConfirmed}
                disabled={resetApplying || resetConfirmText !== 'RESET' || !resetPassword}
              >
                {resetApplying
                  ? t('adminBackup.reset.applying')
                  : t('adminBackup.reset.confirm.submit')}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
