import { adminListSlides, adminUploadSlide } from '../api/admin'
import { isStaffFromToken } from '../api/auth'
import {
  isAbortError,
  isFileTooLargeError,
  isGatewayError,
  isNetworkError,
  isRateLimitError,
  isUnauthorizedError,
} from '../api/shared'
import { clientSafeFilename } from '../safeFilename'
import { acceptedWsiExtensions, exceedsUploadLimit } from './validation'

export type UploadItemStatus =
  | 'queued'
  | 'uploading'
  | 'processing'
  | 'success'
  | 'failed'
  | 'canceled'

// Why an item ended up failed, mapped to a localized message at the call site.
export type UploadFailureKind = 'too-large' | 'ext' | 'rate-limited' | 'processing' | 'network' | 'network-verify' | 'canceled-after-upload'

export interface UploadItem {
  id: string
  file: File
  status: UploadItemStatus
  progress: number
  slideId?: number
  failureKind?: UploadFailureKind
  // Raw error for 'processing' failures (e.g. unreadable WSI). Kept as the
  // thrown object (often an ApiCodeError) rather than a pre-rendered string so
  // the render site can translate it into the active locale via apiErrorMessage.
  error?: unknown
  attempts: number
  // Requeued after a failure whose body had been fully sent (network-verify /
  // canceled-after-upload): the slide may already exist, so the next run
  // looks for it before sending the file again.
  verifyFirst?: boolean
  // When this item was first sent (ms since the epoch): the slide that look
  // is after cannot be older than that.
  firstSentAt?: number
  // That look found the slide: the earlier attempt did land.
  foundOnServer?: boolean
}

// How far the server's clock may be behind this browser's for the created_at
// comparison below.
const CLOCK_SKEW_MS = 10 * 60 * 1000

// The slide an earlier attempt may have created for `file`: the server stores
// it as `{id}_{safe_filename(name)}`, byte for byte, and refuses a second
// slide of the same (case-insensitive) name. So a slide of that name, of
// exactly the file's size and created no earlier than the attempt (`since`,
// less clock skew) is taken to be it — not a same-named slide that was already
// there, nor a different file another admin uploaded under the name. Anything
// that cannot be checked (no attempt time, a missing or unreadable created_at,
// an unrecorded size) is not a match: the caller then re-sends, and a slide
// that did land turns that into a duplicate-name refusal rather than a wrong
// "done". null when there is none; a failed lookup rejects, and the caller
// then just re-sends.
export async function findUploadedSlide(file: File, since?: number): Promise<number | null> {
  if (since == null) return null
  const safe = clientSafeFilename(file.name).toLowerCase()
  const slides = await adminListSlides()
  const hit = slides.find((s) => {
    if ((s.wsi_filename || '').toLowerCase() !== `${s.id}_${safe}`) return false
    if (s.file_size_bytes !== file.size) return false
    const created = Date.parse(s.created_at || '')
    return !Number.isNaN(created) && created >= since - CLOCK_SKEW_MS
  })
  return hit ? hit.id : null
}

const MAX_RETRIES = 4
const BACKOFF_MS = [2000, 4000, 8000, 16000]

let counter = 0
function nextId(): string {
  counter += 1
  return `u${Date.now()}_${counter}`
}

export function fileExtension(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i).toLowerCase() : ''
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve()
    const onAbort = () => { clearTimeout(timer); resolve() }
    const timer = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve() }, ms)
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

// Drives a set of single-file uploads through the existing
// POST /api/admin/slides/upload endpoint one at a time. Sequential execution
// keeps within nginx's 120 req/min API rate limit and avoids piling several
// OpenSlide validations onto the server at once; transient failures (429 /
// transport error) retry with exponential backoff while a real 4xx response
// is terminal for that file — except 401, which stops the whole run (see
// authLost). Each successful upload creates a fresh draft slide row.
export class UploadQueue {
  items: UploadItem[]
  private listeners = new Set<() => void>()
  private controller: AbortController | null = null
  private running = false
  // Set when the run stopped because the login expired (a 401, or a token
  // already past its exp before the next file). Every later file would 401
  // the same way, so the run halts with them left 'queued' — not failed or
  // canceled — and the UI asks the operator to log in again.
  private authLost = false
  // The server's per-file cap (GET /api/admin/storage → max_upload_bytes),
  // set by the screen once it has it; null = not known, so nothing is
  // refused before sending and the server's 413 decides.
  maxUploadBytes: number | null = null
  // The server's accepted extensions (allowed_extensions), likewise set by
  // the screen; null = not known, so the default list applies.
  allowedExtensions: string[] | null = null

  constructor(files: File[]) {
    this.items = files.map((file) => this.makeItem(file))
  }

  private makeItem(file: File): UploadItem {
    return { id: nextId(), file, status: 'queued', progress: 0, attempts: 0 }
  }

  subscribe(fn: () => void): () => void {
    this.listeners.add(fn)
    return () => { this.listeners.delete(fn) }
  }

  private emit() {
    // New array reference so React renders pick up the mutation.
    this.items = [...this.items]
    this.listeners.forEach((fn) => fn())
  }

  get isRunning(): boolean {
    return this.running
  }

  get authExpired(): boolean {
    return this.authLost
  }

  summary() {
    let success = 0
    let failed = 0
    let canceled = 0
    let pending = 0
    for (const it of this.items) {
      if (it.status === 'success') success += 1
      else if (it.status === 'failed') failed += 1
      else if (it.status === 'canceled') canceled += 1
      else pending += 1
    }
    const total = this.items.length
    return { success, failed, canceled, pending, done: total - pending, total }
  }

  addFiles(files: File[]) {
    if (this.running || files.length === 0) return
    this.items = [...this.items, ...files.map((f) => this.makeItem(f))]
    this.emit()
  }

  removeItem(id: string) {
    if (this.running) return
    this.items = this.items.filter((x) => x.id !== id)
    this.emit()
  }

  cancelAll() {
    this.controller?.abort()
    for (const it of this.items) {
      if (it.status === 'queued') it.status = 'canceled'
    }
    this.emit()
  }

  // Reset failed/canceled items back to queued so a subsequent start() picks
  // them up. The caller is responsible for calling start() afterwards.
  requeueFailed(): boolean {
    let any = false
    for (const it of this.items) {
      if (it.status === 'failed' || it.status === 'canceled') {
        it.status = 'queued'
        it.progress = 0
        it.attempts = 0
        it.verifyFirst = it.failureKind === 'network-verify' || it.failureKind === 'canceled-after-upload'
        it.failureKind = undefined
        it.error = undefined
        any = true
      }
    }
    if (any) this.emit()
    return any
  }

  async start(): Promise<void> {
    if (this.running) return
    this.running = true
    this.authLost = false
    this.controller = new AbortController()
    const signal = this.controller.signal
    this.emit()
    try {
      // Snapshot ids up front: addFiles is disabled while running, so the set
      // is stable, but iterating ids (not the array) stays correct across the
      // emit()-driven array replacement inside the loop.
      const ids = this.items.map((it) => it.id)
      for (const id of ids) {
        if (signal.aborted || this.authLost) break
        const item = this.items.find((it) => it.id === id)
        if (!item || item.status !== 'queued') continue
        // Don't start a multi-GB transfer on a token that has already expired:
        // the server would refuse it, possibly only after the body was sent.
        if (!isStaffFromToken()) { this.authLost = true; break }
        await this.processItem(item, signal)
      }
    } finally {
      if (signal.aborted) {
        for (const it of this.items) {
          if (it.status === 'queued' || it.status === 'uploading' || it.status === 'processing') {
            it.status = 'canceled'
          }
        }
      }
      this.running = false
      this.controller = null
      this.emit()
    }
  }

  private preValidate(item: UploadItem): UploadFailureKind | null {
    if (!acceptedWsiExtensions(this.allowedExtensions).includes(fileExtension(item.file.name))) return 'ext'
    if (exceedsUploadLimit(item.file.size, this.maxUploadBytes)) return 'too-large'
    return null
  }

  private async processItem(item: UploadItem, signal: AbortSignal): Promise<void> {
    const pre = this.preValidate(item)
    if (pre) {
      item.status = 'failed'
      item.failureKind = pre
      this.emit()
      return
    }

    if (item.verifyFirst) {
      item.verifyFirst = false
      item.status = 'processing'
      this.emit()
      let existing: number | null = null
      try {
        existing = await findUploadedSlide(item.file, item.firstSentAt)
      } catch {
        // Unknown: send it again. If the slide did land, the server refuses
        // the duplicate name (409) without keeping a second copy.
      }
      if (existing != null) {
        item.status = 'success'
        item.slideId = existing
        item.progress = 100
        item.foundOnServer = true
        this.emit()
        return
      }
    }

    for (let attempt = 0; ; attempt += 1) {
      if (signal.aborted) {
        item.status = 'canceled'
        this.emit()
        return
      }
      item.attempts = attempt + 1
      item.firstSentAt ??= Date.now()
      item.status = 'uploading'
      item.progress = 0
      this.emit()
      // Track whether the request body was fully transmitted this attempt. A
      // network error AFTER that point is NOT safely retryable: the server may
      // have already committed the draft slide and only the response was lost,
      // so re-POSTing would create a duplicate. Taken from onSent (the
      // upload's own load event), not from a progress figure: one that
      // rounded up to 100 short of the end would make a drop in the last few MB
      // non-retryable.
      let fullyUploaded = false
      try {
        const res = await adminUploadSlide(item.file, {
          signal,
          onProgress: (p) => {
            item.progress = p
            this.emit()
          },
          onSent: () => {
            fullyUploaded = true
            item.status = 'processing'
            this.emit()
          },
        })
        item.status = 'success'
        item.slideId = res.slide_id
        item.progress = 100
        this.emit()
        return
      } catch (err) {
        if (signal.aborted || isAbortError(err)) {
          // Aborting after the body was fully sent only drops the response:
          // the server may still commit the draft slide, so a plain
          // 'canceled' would invite a retry that 409s on the duplicate
          // filename. Surface it for manual verification instead.
          if (fullyUploaded) {
            item.status = 'failed'
            item.failureKind = 'canceled-after-upload'
          } else {
            item.status = 'canceled'
          }
          this.emit()
          return
        }
        // Login expired / revoked mid-run. The server refused the request, so
        // no slide was created: put the file back in the queue and stop.
        if (isUnauthorizedError(err)) {
          item.status = 'queued'
          item.progress = 0
          this.authLost = true
          this.emit()
          return
        }
        if (isFileTooLargeError(err)) {
          item.status = 'failed'
          item.failureKind = 'too-large'
          this.emit()
          return
        }
        // A dropped connection after the body was fully sent is non-retryable
        // (would risk a duplicate slide); surface it for manual verification.
        // A proxy's own 502 / 504 / 52x (a read timeout while the server was
        // still validating the file) says no more than a drop does.
        if ((isNetworkError(err) || isGatewayError(err)) && fullyUploaded) {
          item.status = 'failed'
          item.failureKind = 'network-verify'
          this.emit()
          return
        }
        const transient = isRateLimitError(err) || isNetworkError(err) || isGatewayError(err)
        if (transient && attempt < MAX_RETRIES) {
          await delay(BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)], signal)
          continue
        }
        item.status = 'failed'
        if (isRateLimitError(err)) item.failureKind = 'rate-limited'
        else if (isNetworkError(err) || isGatewayError(err)) item.failureKind = 'network'
        else {
          item.failureKind = 'processing'
          item.error = err
        }
        this.emit()
        return
      }
    }
  }
}
