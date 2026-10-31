// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { STORAGE_KEYS } from '../storageKeys'
import {
  _resetLoginRedirectForTests,
  ApiCodeError,
  isAbortError,
  isApiCodeError,
  isFileTooLargeError,
  isNetworkError,
  isRateLimitError,
  isUnauthorizedError,
  xhrUpload,
} from './shared'

// XMLHttpRequest double. `answer` decides how the request ends once sent:
// an HTTP status + body, a transport failure, or nothing (still in flight).
type Answer = { status: number; body: string } | 'network-error' | 'pending'

class FakeXhr {
  static answer: Answer = { status: 200, body: '{}' }
  static last: FakeXhr | null = null
  headers: Record<string, string> = {}
  method = ''
  url = ''
  sent: unknown = null
  status = 0
  // UNSENT -> OPENED on open(); HEADERS_RECEIVED (2) once an answer starts.
  readyState = 0
  responseText = ''
  aborted = false
  upload: {
    onprogress?: (e: { lengthComputable: boolean; loaded: number; total: number }) => void
    onload?: () => void
  } = {}
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  onabort: (() => void) | null = null
  constructor() { FakeXhr.last = this }
  open(method: string, url: string) { this.method = method; this.url = url; this.readyState = 1 }
  setRequestHeader(k: string, v: string) { this.headers[k] = v }
  abort() {
    this.aborted = true
    this.onabort?.()
  }
  send(body: unknown) {
    this.sent = body
    const answer = FakeXhr.answer
    if (answer === 'pending') return
    queueMicrotask(() => {
      if (answer === 'network-error') { this.onerror?.(); return }
      this.upload.onprogress?.({ lengthComputable: true, loaded: 1, total: 2 })
      this.upload.onload?.()
      this.status = answer.status
      this.responseText = answer.body
      this.onload?.()
    })
  }
  // Test helpers driving a 'pending' request.
  finishBody() { this.upload.onload?.() }
  // An answer that arrives before the body is fully sent (an early 413 /
  // 401 / 429): some engines then end the upload phase with its load event
  // although the body never all went out.
  answerEarly(status: number, body: string) {
    this.readyState = 2
    this.status = status
    this.upload.onload?.()
    this.respond(status, body)
  }
  respond(status: number, body: string) {
    this.readyState = 4
    this.status = status
    this.responseText = body
    this.onload?.()
  }
}

function upload(opts: Parameters<typeof xhrUpload>[3] = {}) {
  vi.stubGlobal('XMLHttpRequest', FakeXhr)
  const form = new FormData()
  form.append('file', new File(['x'], 'a.svs'))
  return xhrUpload<{ job_id?: string }>('/api/admin/x', form, 'x upload failed', opts)
}

function answering(status: number, body: string) {
  FakeXhr.answer = { status, body }
  return upload().catch((e) => e)
}

afterEach(() => {
  FakeXhr.answer = { status: 200, body: '{}' }
  FakeXhr.last = null
  sessionStorage.clear()
  _resetLoginRedirectForTests()
  vi.unstubAllGlobals()
})

describe('xhrUpload success', () => {
  it('posts the form with the staff token and resolves with the JSON answer', async () => {
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 'tok')
    FakeXhr.answer = { status: 202, body: '{"job_id":"j-1"}' }
    const progress: number[] = []
    await expect(upload({ onProgress: (p) => progress.push(p) })).resolves.toEqual({ job_id: 'j-1' })
    const xhr = FakeXhr.last!
    expect(xhr.method).toBe('POST')
    expect(xhr.url).toBe('/api/admin/x')
    expect(xhr.sent).toBeInstanceOf(FormData)
    expect(xhr.headers).toEqual({ Authorization: 'Bearer tok' })
    expect(progress).toEqual([50, 100])
  })

  it('sends no Authorization header without a token', async () => {
    await upload()
    expect(FakeXhr.last!.headers).toEqual({})
  })

  it('rejects a 2xx whose body is not JSON', async () => {
    const err = await answering(200, '<html></html>')
    expect(err.message).toBe('Server returned non-JSON response')
  })
})

describe('xhrUpload progress', () => {
  it('reports 100 only once the whole body is sent, never from a rounded figure', async () => {
    FakeXhr.answer = 'pending'
    const events: string[] = []
    const p = upload({ onProgress: (pct) => events.push(String(pct)), onSent: () => events.push('sent') })
    const xhr = FakeXhr.last!
    const progress = (loaded: number) => xhr.upload.onprogress?.({ lengthComputable: true, loaded, total: 1000 })
    progress(6)
    // 99.6 %: rounding would say 100 here, and callers would take that for "sent" with
    // 0.4 % of the body (~20 MB of a 5 GB file) still to go.
    progress(996)
    // The last progress event comes just before the upload's load event.
    progress(1000)
    expect(events).toEqual(['0', '99', '99'])
    xhr.finishBody()
    expect(events).toEqual(['0', '99', '99', '100', 'sent'])
    xhr.respond(202, '{"job_id":"j-4"}')
    await expect(p).resolves.toEqual({ job_id: 'j-4' })
    expect(events).toEqual(['0', '99', '99', '100', 'sent'])
  })

  it('does not report the body as sent when the answer came first', async () => {
    FakeXhr.answer = 'pending'
    const events: string[] = []
    const p = upload({ onProgress: (pct) => events.push(String(pct)), onSent: () => events.push('sent') })
    FakeXhr.last!.upload.onprogress?.({ lengthComputable: true, loaded: 10, total: 1000 })
    FakeXhr.last!.answerEarly(413, '<html>413</html>')
    expect(isFileTooLargeError(await p.catch((e) => e))).toBe(true)
    // No "processing" flip, no 100 %, before the error.
    expect(events).toEqual(['1'])
  })

  it('still reports 100 for a 2xx answer that came without an upload load event', async () => {
    FakeXhr.answer = 'pending'
    const progress: number[] = []
    const p = upload({ onProgress: (pct) => progress.push(pct) })
    FakeXhr.last!.respond(200, '{}')
    await p
    expect(progress).toEqual([100])
  })
})

describe('xhrUpload errors are typed like throwApi', () => {
  it('413 → FileTooLargeError (nginx answers with HTML)', async () => {
    expect(isFileTooLargeError(await answering(413, '<html>413</html>'))).toBe(true)
  })

  it('429 → RateLimitError', async () => {
    expect(isRateLimitError(await answering(429, '<html>429</html>'))).toBe(true)
  })

  it('401 → UnauthorizedError, dropping a rejected staff token', async () => {
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 'stale')
    const replace = vi.fn()
    const realLocation = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...realLocation, pathname: '/admin/dashboard', replace },
    })
    try {
      expect(isUnauthorizedError(await answering(401, '{"detail":"Invalid token"}'))).toBe(true)
      expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBeNull()
      expect(replace).toHaveBeenCalledWith('/admin')
      // Any other 401 is still an auth failure, without the redirect.
      expect(isUnauthorizedError(await answering(401, '<html>401</html>'))).toBe(true)
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: realLocation })
    }
  })

  it('no HTTP answer → NetworkError', async () => {
    FakeXhr.answer = 'network-error'
    expect(isNetworkError(await upload().catch((e) => e))).toBe(true)
  })

  it('a coded JSON error → ApiCodeError with its params', async () => {
    const err = await answering(507, '{"detail":{"code":"adminDash.apiErr.uploadNoSpace","params":{"free":"1 GB"}}}')
    expect(isApiCodeError(err)).toBe(true)
    expect(err.code).toBe('adminDash.apiErr.uploadNoSpace')
    expect(err.params).toEqual({ free: '1 GB' })
  })

  it('a string or validation-list detail becomes the message', async () => {
    expect((await answering(400, '{"detail":"Duplicate filename"}')).message).toBe('Duplicate filename')
    const list = await answering(422, '{"detail":[{"msg":"field required"},{"msg":"too long"}]}')
    expect(list.message).toBe('field required, too long')
  })

  it("a gateway's HTML 5xx carries no detail: the fallback, or the caller's gateway error", async () => {
    expect((await answering(502, '<html>bad gateway</html>')).message).toBe('x upload failed')
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    for (const status of [502, 503, 504]) {
      FakeXhr.answer = { status, body: '<html>gateway</html>' }
      const err = await upload({ gatewayError: (s) => new ApiCodeError('gw', { status: s }) }).catch((e) => e)
      expect(err.code).toBe('gw')
      expect(err.params).toEqual({ status })
    }
    // The app's own JSON 503 is a definite answer, not the gateway's.
    FakeXhr.answer = { status: 503, body: '{"detail":"busy"}' }
    const own = await upload({ gatewayError: () => new ApiCodeError('gw') }).catch((e) => e)
    expect(own.message).toBe('busy')
    // A 500 is not a gateway failure.
    FakeXhr.answer = { status: 500, body: '<html>500</html>' }
    const plain = await upload({ gatewayError: () => new ApiCodeError('gw') }).catch((e) => e)
    expect(plain.message).toBe('x upload failed')
  })

  it("Cloudflare's 520-524 pages are the gateway's too (524: its 100 s response timeout)", async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    for (const status of [520, 521, 522, 523, 524]) {
      FakeXhr.answer = { status, body: '<!DOCTYPE html><title>A timeout occurred</title>' }
      const err = await upload({ gatewayError: (s) => new ApiCodeError('gw', { status: s }) }).catch((e) => e)
      expect(err.code).toBe('gw')
      expect(err.params).toEqual({ status })
    }
    // 525 / 526 (TLS to the origin failed) are definite, not "no answer yet".
    FakeXhr.answer = { status: 525, body: '<html>525</html>' }
    const tls = await upload({ gatewayError: () => new ApiCodeError('gw') }).catch((e) => e)
    expect(tls.message).toBe('x upload failed')
  })
})

describe('xhrUpload abort', () => {
  it('rejects at once with an AbortError when the signal already aborted', async () => {
    const ctrl = new AbortController()
    ctrl.abort()
    expect(isAbortError(await upload({ signal: ctrl.signal }).catch((e) => e))).toBe(true)
    expect(FakeXhr.last).toBeNull()
  })

  it('aborts the request in flight', async () => {
    FakeXhr.answer = 'pending'
    const ctrl = new AbortController()
    const p = upload({ signal: ctrl.signal }).catch((e) => e)
    ctrl.abort()
    expect(isAbortError(await p)).toBe(true)
    expect(FakeXhr.last!.aborted).toBe(true)
  })

  it('with keepAfterSent, aborts while the body is sending but not once it is sent', async () => {
    FakeXhr.answer = 'pending'
    const early = new AbortController()
    const p1 = upload({ signal: early.signal, keepAfterSent: true }).catch((e) => e)
    early.abort()
    expect(isAbortError(await p1)).toBe(true)

    const late = new AbortController()
    const p2 = upload({ signal: late.signal, keepAfterSent: true })
    const xhr = FakeXhr.last!
    xhr.finishBody()
    late.abort()
    expect(xhr.aborted).toBe(false)
    xhr.respond(202, '{"job_id":"j-2"}')
    await expect(p2).resolves.toEqual({ job_id: 'j-2' })
  })

  it('reports when the body has been sent, before the answer', async () => {
    FakeXhr.answer = 'pending'
    const onSent = vi.fn()
    const p = upload({ onSent })
    const xhr = FakeXhr.last!
    expect(onSent).not.toHaveBeenCalled()
    xhr.finishBody()
    expect(onSent).toHaveBeenCalledTimes(1)
    xhr.respond(202, '{"job_id":"j-3"}')
    await expect(p).resolves.toEqual({ job_id: 'j-3' })
    expect(onSent).toHaveBeenCalledTimes(1)
  })
})
