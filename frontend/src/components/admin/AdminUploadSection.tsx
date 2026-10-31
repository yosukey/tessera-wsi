import React, { useCallback, useEffect, useReducer, useRef, useState, type MutableRefObject } from 'react'
import { useUnsavedNavigationGuard } from '../../hooks/useUnsavedNavigationGuard'
import { useNavigate } from 'react-router'
import {
  adminGetMyIp,
  adminGetStorage,
  clearToken,
  isAbortError,
  isAdminFromToken,
  isManagerOrAboveFromToken,
  type StorageInfo,
} from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { logger } from '../../lib/logger'
import {
  acceptedWsiExtensions,
  MAX_BULK_UPLOAD_FILES,
  exceedsUploadLimit,
  formatByteLimit,
  formatBytes,
} from '../../lib/admin/validation'
import { simulateUploadSpace, type UploadSpaceShortfall } from '../../lib/admin/uploadSpace'
import { UploadQueue, fileExtension, type UploadItem } from '../../lib/admin/uploadQueue'
import { apiErrorMessage } from '../../lib/admin/apiError'
import { copyTextToClipboard } from '../../lib/clipboard'
import PhaseStatus from './PhaseStatus'

const TUNNEL_WARN_STYLE: React.CSSProperties = {
  padding: 'var(--space-2) var(--space-3)',
  borderRadius: 4,
  background: 'rgba(220, 150, 0, 0.1)',
  color: 'var(--color-warning-text, #7d4e00)',
  fontSize: 13,
  marginBottom: 8,
}

interface Props {
  refreshSlides: () => Promise<void>
  // Lets the dashboard lock sidebar tab switching while an upload is in
  // flight. The queue lives in this component's local state, so leaving
  // the upload tab unmounts it and drops the in-progress batch.
  onRunningChange?: (running: boolean) => void
  // Holds the navigation guard's in-app exit while this section is mounted
  // (null otherwise). The guard's history entry outlives the run that pushed
  // it, so the dashboard leaves the tab through this — header links, Logout,
  // a sidebar tab switch — to step off that entry first instead of leaving it
  // behind for Back to land on.
  leaveRef?: MutableRefObject<((go: () => void) => void) | null>
}

const STATUS_COLORS: Record<UploadItem['status'], string> = {
  queued: 'var(--color-border-strong)',
  uploading: 'var(--color-brand-600)',
  processing: 'var(--color-warning-fg)',
  success: 'var(--color-success-fg, #1a7f37)',
  failed: 'var(--color-danger-fg, #b42318)',
  canceled: 'var(--color-border-strong)',
}

function AdminUploadSectionInner({ refreshSlides, onRunningChange, leaveRef }: Props) {
  const { t } = useI18n()
  const nav = useNavigate()
  const [queue, setQueue] = useState<UploadQueue | null>(null)
  const [, forceRender] = useReducer((x: number) => x + 1, 0)
  const [viaTunnel, setViaTunnel] = useState(false)
  const [copied, setCopied] = useState(false)
  const [storage, setStorage] = useState<StorageInfo | null>(null)
  const [selectMsg, setSelectMsg] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    // /api/admin/network/my-ip is admin-only; for a manager it can only 403.
    if (!isAdminFromToken()) return
    const ctrl = new AbortController()
    adminGetMyIp({ signal: ctrl.signal })
      .then((r) => setViaTunnel(r.via_cloudflare_tunnel))
      .catch((err) => { if (!isAbortError(err)) { /* ignore — banner is best-effort */ } })
    return () => ctrl.abort()
  }, [])

  const loadStorage = useCallback(async (signal?: AbortSignal) => {
    try {
      setStorage(await adminGetStorage({ signal }))
    } catch (err) {
      if (!isAbortError(err)) logger.error('storage info failed:', err)
    }
  }, [])

  useEffect(() => {
    const ctrl = new AbortController()
    void loadStorage(ctrl.signal)
    return () => ctrl.abort()
  }, [loadStorage])

  useEffect(() => {
    if (!queue) return
    return queue.subscribe(() => forceRender())
  }, [queue])

  // Leaving the dashboard (SPA navigation / browser Back) unmounts this UI
  // while the queue keeps going, uploading invisibly: the slide list never
  // shows the drafts it creates and re-selecting the files then 409s on the
  // duplicate filename. Abort the run on unmount instead. The tab lock keeps
  // a sidebar switch from reaching here mid-run.
  const queueRef = useRef<UploadQueue | null>(null)
  queueRef.current = queue
  useEffect(() => () => {
    const q = queueRef.current
    if (q?.isRunning) q.cancelAll()
  }, [])

  const running = queue?.isRunning ?? false
  // The server's per-file cap; null until /api/admin/storage has answered
  // (or when it could not), and then nothing is refused before sending.
  const maxUploadBytes = storage?.max_upload_bytes ?? null
  // Likewise the server's extension list (the default one until it is known).
  const allowedExtensions = acceptedWsiExtensions(storage?.allowed_extensions)

  // Report upload activity up so the dashboard can lock sidebar tabs. Reset
  // to false on unmount so a stuck "uploading" lock can't outlive this
  // component if it ever unmounts mid-flight.
  useEffect(() => {
    onRunningChange?.(running)
    return () => onRunningChange?.(false)
  }, [running, onRunningChange])

  // Browser Back / Forward leaves the dashboard without firing beforeunload, so
  // the guard below is what asks first (without it, an unconfirmed Back would
  // abort the run silently via the unmount cleanup above).
  const leave = useUnsavedNavigationGuard(running, t('adminDash.uploadLeaveConfirm'))
  useEffect(() => {
    if (!leaveRef) return
    leaveRef.current = leave
    return () => {
      if (leaveRef.current === leave) leaveRef.current = null
    }
  }, [leaveRef, leave])

  // Warn before leaving while uploads are in flight; aborting a request mid
  // upload is safe server-side (the placeholder row is rolled back) but the
  // operator likely wants to finish the batch.
  useEffect(() => {
    if (!running) return
    const handler = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [running])

  function onFilesSelected(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? [])
    // Reset the input so re-selecting the same file fires onChange again.
    e.target.value = ''
    if (files.length === 0) return
    setCopied(false)
    const current = queue && !queue.isRunning ? queue.items.length : 0
    if (current + files.length > MAX_BULK_UPLOAD_FILES) {
      setSelectMsg(t('adminDash.bulkMaxFilesExceeded', { max: MAX_BULK_UPLOAD_FILES }))
      return
    }
    setSelectMsg(null)
    if (queue && !queue.isRunning) {
      queue.addFiles(files)
    } else if (!queue) {
      setQueue(new UploadQueue(files))
    }
    void loadStorage()
  }

  function runQueue(q: UploadQueue) {
    q.maxUploadBytes = maxUploadBytes
    q.allowedExtensions = storage?.allowed_extensions ?? null
    q.start()
      .then(() => refreshSlides())
      .catch((err) => { logger.error('refreshSlides after bulk upload failed:', err) })
      .finally(() => { void loadStorage() })
  }

  function onStart() {
    if (queue) runQueue(queue)
  }

  function onRetryFailed() {
    if (queue && queue.requeueFailed()) {
      setCopied(false)
      runQueue(queue)
    }
  }

  function onReset() {
    setQueue(null)
    setCopied(false)
    setSelectMsg(null)
  }

  function failureText(it: UploadItem): string {
    switch (it.failureKind) {
      case 'too-large': return maxUploadBytes != null
        ? t('adminDash.fileSizeExceeded', { limit: formatByteLimit(maxUploadBytes) })
        : t('common.fileTooLargeServer')
      case 'ext': return t('adminDash.fileExtNotSupported', { ext: fileExtension(it.file.name) || '?' })
      case 'rate-limited': return t('common.rateLimited')
      case 'processing': return apiErrorMessage(it.error, t) || t('adminDash.uploadFailed')
      case 'network': return t('adminDash.uploadFailed')
      case 'network-verify': return t('adminDash.uploadNetworkVerify')
      case 'canceled-after-upload': return t('adminDash.uploadCanceledAfterUpload')
      default: return t('adminDash.uploadFailed')
    }
  }

  function statusLabel(it: UploadItem): string {
    switch (it.status) {
      case 'queued': return t('adminDash.bulkStatusQueued')
      case 'uploading': return t('adminDash.transferring', { percent: it.progress })
      case 'processing': return t('adminDash.bulkStatusProcessing')
      case 'success': return it.foundOnServer ? t('adminDash.bulkStatusFoundOnServer') : t('adminDash.bulkStatusSuccess')
      case 'failed': return t('adminDash.bulkStatusFailed')
      case 'canceled': return t('adminDash.bulkStatusCanceled')
    }
  }

  async function onCopyFailures() {
    if (!queue) return
    const lines = queue.items
      .filter((it) => it.status === 'failed' || it.status === 'canceled')
      .map((it) => `${it.file.name}\t${it.status === 'canceled' ? t('adminDash.bulkStatusCanceled') : failureText(it)}`)
    try {
      // copyTextToClipboard, not navigator.clipboard directly: the Async
      // Clipboard API is undefined outside a secure context, so on a plain-HTTP
      // LAN deployment it throws TypeError into the catch below.
      await copyTextToClipboard(lines.join('\n'))
      setCopied(true)
    } catch (err) {
      logger.error('copy failures failed:', err)
    }
  }

  const items = queue?.items ?? []
  const summary = queue?.summary()
  const selectedBytes = items.reduce((sum, it) => sum + it.file.size, 0)
  const showSummaryPanel = !!queue && !running && !!summary && summary.done > 0
  // The run stopped on an expired login; the rest of the queue is untouched.
  const authExpired = !!queue && !running && queue.authExpired
  const failedItems = items.filter((it) => it.status === 'failed' || it.status === 'canceled')

  // Free-space preflight: the server's own per-file rule (spool + staged copy
  // plus the shared reserve, on the volumes it really measures), simulated
  // over the batch in upload order with each file's staged bytes taken off
  // the free space the next one sees — see lib/admin/uploadSpace.ts. A rule of
  // its own such as "queued bytes + 1 GiB <= data-dir free" could disagree with
  // the server's in both directions. Unknown free space (no
  // figures, or a volume the server cannot measure) never blocks, as on the
  // server. Only files the queue will actually send count: its own
  // pre-validation fails a wrong extension / oversize file without a request.
  const willSend = (it: UploadItem) =>
    allowedExtensions.includes(fileExtension(it.file.name)) && !exceedsUploadLimit(it.file.size, maxUploadBytes)
  const spaceFor = (batch: UploadItem[]): UploadSpaceShortfall | null =>
    simulateUploadSpace(batch.filter(willSend).map((it) => it.file), storage?.upload_space)
  const queuedItems = items.filter((it) => it.status === 'queued')
  const queuedBytes = queuedItems.reduce((sum, it) => sum + it.file.size, 0)
  const queuedShort = queuedItems.length > 0 ? spaceFor(queuedItems) : null
  // "Retry failed" requeues the failed / canceled items and start() then runs
  // every queued item in list order (queued ones survive an auth stop), so
  // gate that button on that batch — less the items whose bytes most likely
  // already are on the server: 'canceled-after-upload' and 'network-verify'
  // both mean the body was fully sent and only the answer was lost, so the
  // server probably committed the slide, and its staged WSI is already
  // inside the free figure the post-run storage refresh reports. Counting
  // its bytes again would double-count them and could disable Retry for no
  // reason. Retry first looks such an item up in the slide list and marks it
  // done when its slide is there (UploadQueue.verifyFirst); only otherwise is
  // it re-sent: if the slide did land after all, the server refuses
  // the duplicate filename (409) without keeping a second copy; if it did
  // not, the upload goes through and the server's own per-file space check
  // still guards it — so leaving it out here can only under-block, never
  // lose data.
  const probablyOnServer = (it: UploadItem) =>
    it.failureKind === 'canceled-after-upload' || it.failureKind === 'network-verify'
  const retryVisible = !running && !!summary && summary.failed + summary.canceled > 0
  const retryShort = retryVisible
    ? spaceFor(items.filter((it) =>
      it.status === 'queued'
      || ((it.status === 'failed' || it.status === 'canceled') && !probablyOnServer(it))))
    : null
  const insufficientSpace = queuedShort != null
  // One warning, for the action it blocks: the queued batch's when "Start"
  // is the blocked one (that batch is a prefix of the retry's, so it blocks
  // Retry too), else the retry's — worded for Retry, since "Start" may well
  // be enabled then (the queued files fit; only adding the failed ones back
  // does not) and the generic wording read as if it blocked the start.
  const spaceWarning = queuedShort
    ? { short: queuedShort, forRetry: false }
    : retryShort
      ? { short: retryShort, forRetry: true }
      : null
  const spaceWarningText = spaceWarning
    ? t(
      spaceWarning.forRetry ? 'adminDash.bulkRetryInsufficientSpace' : 'adminDash.bulkInsufficientSpace',
      { file: spaceWarning.short.name, short: formatBytes(spaceWarning.short.shortfallBytes) },
    )
    : null
  const freeBytes = storage && storage.free_bytes > 0 ? storage.free_bytes : null
  const projectedFreeBytes = freeBytes != null ? Math.max(0, freeBytes - queuedBytes) : null

  return (
    <>
      <h3 style={{ marginTop: 16 }}>{t('adminDash.uploadWsi')}</h3>
      <div style={{ display: 'grid', gap: 10 }}>
        <div style={{ borderLeft: '3px solid var(--color-border-strong)', paddingLeft: 8, marginTop: 4 }}>
          <div style={{ fontSize: 13, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'inherit', marginBottom: 8 }}>
            {t('adminDash.sectionUpload')}
          </div>
          <div style={{ display: 'grid', gap: 8 }}>
            {viaTunnel && (
              <div style={TUNNEL_WARN_STYLE}>{t('cloudflare.tunnel.warning')}</div>
            )}
            <div style={{ fontSize: 12, opacity: 0.8 }}>{t('adminDash.uploadAutoIdHelp')}</div>
            <div>
              <input
                ref={fileInputRef}
                name="file"
                type="file"
                multiple
                accept={allowedExtensions.join(',')}
                disabled={running}
                onChange={onFilesSelected}
                style={{ boxSizing: 'border-box', width: '100%' }}
              />
              <div style={{ fontSize: 12, opacity: 0.8, marginTop: 3, wordBreak: 'break-all' }}>
                {maxUploadBytes != null
                  ? t('adminDash.allowedExtensions', { exts: allowedExtensions.join(', '), limit: formatByteLimit(maxUploadBytes) })
                  : t('adminDash.allowedExtensionsOnly', { exts: allowedExtensions.join(', ') })}
                {' · '}
                {t('adminDash.bulkMaxFilesHint', { max: MAX_BULK_UPLOAD_FILES })}
              </div>
              <div style={{ fontSize: 12, opacity: 0.8, marginTop: 3 }}>
                {t('adminDash.omeTiffHint')}
              </div>
            </div>

            {selectMsg && (
              <div style={{ fontSize: 12, padding: '4px 6px', borderRadius: 4, background: 'var(--color-danger-bg)' }}>{selectMsg}</div>
            )}

            {items.length > 0 && (
              <>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', fontSize: 13 }}>
                  <span style={{ fontWeight: 600 }}>
                    {t('adminDash.bulkSelectedCount', { count: items.length, size: formatBytes(selectedBytes) })}
                  </span>
                  {running && summary && (
                    <span style={{ opacity: 0.8 }}>{t('adminDash.bulkOverall', { done: summary.done, total: summary.total })}</span>
                  )}
                </div>

                <div style={{ fontSize: 12, opacity: 0.85 }}>
                  {storage
                    ? (freeBytes != null
                        ? t('adminDash.bulkStorageLine', { free: formatBytes(freeBytes), total: formatBytes(storage.total_bytes) })
                        : t('adminDash.bulkStorageUnknown'))
                    : t('adminDash.bulkStorageUnknown')}
                  {projectedFreeBytes != null && queuedBytes > 0 && (
                    <> {' · '}{t('adminDash.bulkProjectedFree', { free: formatBytes(projectedFreeBytes) })}</>
                  )}
                </div>
                {spaceWarningText && !running && (
                  // Polite, and announced once per distinct message: as a
                  // role="alert" it would interrupt the screen reader on every
                  // file added or removed (and on each storage refresh) while
                  // the batch stayed short. It blocks nothing the operator is
                  // doing right now — the disabled button says the rest.
                  <div style={{ fontSize: 12, padding: '4px 6px', borderRadius: 4, background: 'var(--color-danger-bg)' }}>
                    <PhaseStatus text={spaceWarningText} phase={spaceWarningText} />
                  </div>
                )}

                {authExpired && (
                  <div role="alert" style={{ fontSize: 12, padding: '6px 8px', borderRadius: 4, background: 'var(--color-danger-bg)', display: 'grid', gap: 6 }}>
                    <div>{t('adminDash.bulkAuthExpired')}</div>
                    <div>
                      <button type="button" onClick={() => { clearToken(); leave(() => nav('/admin')) }} style={{ fontSize: 12 }}>
                        {t('adminDash.bulkAuthExpiredLogin')}
                      </button>
                    </div>
                  </div>
                )}

                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {/* Hidden after an auth stop: every file would 401 again. */}
                  {!running && !authExpired && summary && summary.pending > 0 && (
                    <button type="button" onClick={onStart} disabled={insufficientSpace}>{t('adminDash.bulkStart')}</button>
                  )}
                  {running && (
                    <button type="button" onClick={() => queue?.cancelAll()}>{t('adminDash.bulkCancelAll')}</button>
                  )}
                  {retryVisible && (
                    <button type="button" onClick={onRetryFailed} disabled={retryShort != null}>{t('adminDash.bulkRetryFailed')}</button>
                  )}
                  {!running && (
                    <button type="button" onClick={onReset}>{t('adminDash.bulkClear')}</button>
                  )}
                </div>

                <div style={{ display: 'grid', gap: 4 }}>
                  {items.map((it) => (
                    <div
                      key={it.id}
                      style={{
                        display: 'grid',
                        gridTemplateColumns: '1fr auto',
                        gap: 8,
                        alignItems: 'center',
                        padding: '6px 8px',
                        border: '1px solid var(--color-border)',
                        borderRadius: 4,
                        fontSize: 12,
                      }}
                    >
                      <div style={{ minWidth: 0 }}>
                        <div style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
                          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={it.file.name}>{it.file.name}</span>
                          <span style={{ opacity: 0.6, flexShrink: 0 }}>{formatBytes(it.file.size)}</span>
                        </div>
                        {(it.status === 'uploading') && (
                          <progress value={it.progress} max={100} style={{ width: '100%', height: 6, marginTop: 3 }} />
                        )}
                        {it.status === 'processing' && (
                          <progress max={100} style={{ width: '100%', height: 6, marginTop: 3 }} />
                        )}
                        {(it.status === 'failed') && (
                          <div style={{ color: STATUS_COLORS.failed, marginTop: 2 }}>{failureText(it)}</div>
                        )}
                      </div>
                      <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexShrink: 0 }}>
                        <span style={{ color: STATUS_COLORS[it.status], fontWeight: 600 }}>{statusLabel(it)}</span>
                        {!running && (it.status === 'queued' || it.status === 'failed' || it.status === 'canceled') && (
                          <button type="button" onClick={() => queue?.removeItem(it.id)} style={{ fontSize: 11 }}>
                            {t('adminDash.bulkRemove')}
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}

            {showSummaryPanel && summary && (
              <div style={{ marginTop: 4, padding: '8px 10px', borderRadius: 4, background: summary.failed + summary.canceled > 0 ? 'var(--color-danger-bg)' : 'var(--color-success-bg)' }}>
                <div style={{ fontWeight: 700, fontSize: 13 }}>{t('adminDash.bulkSummaryTitle')}</div>
                <div style={{ fontSize: 12, marginTop: 2 }}>
                  {t('adminDash.bulkSummaryCounts', { success: summary.success, failed: summary.failed, canceled: summary.canceled })}
                </div>
                {summary.success > 0 && (
                  <div style={{ fontSize: 12, marginTop: 6, opacity: 0.9 }}>{t('adminDash.bulkDraftNote')}</div>
                )}
                {failedItems.length > 0 && (
                  <div style={{ marginTop: 6 }}>
                    <div style={{ fontSize: 12, fontWeight: 600 }}>{t('adminDash.bulkFailedTitle')}</div>
                    <button type="button" onClick={onCopyFailures} style={{ fontSize: 11, marginTop: 4 }}>
                      {copied ? t('adminDash.bulkCopied') : t('adminDash.bulkCopyErrors')}
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  )
}

export default function AdminUploadSection(props: Props) {
  // Hide the entire uploader for non-manager roles. Instructors land on
  // this tab as their "Slides" home; the dashboard still renders the
  // slide list in <main>, so this component renders nothing.
  //
  // Decided once, at mount: isManagerOrAboveFromToken() is false as soon as
  // the token's exp passes, and re-checking it on every render would make the
  // uploader — queue, per-file results and all — vanish at the end of a run
  // that outlived the login, instead of showing what was left to upload.
  const [allowed] = useState(isManagerOrAboveFromToken)
  if (!allowed) return null
  return <AdminUploadSectionInner {...props} />
}
