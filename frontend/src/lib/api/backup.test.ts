// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { adminRestoreFromUpload } from './backup'
import { STORAGE_KEYS } from '../storageKeys'

// Minimal XHR double: records request headers, then answers 200 with a stage.
class FakeXhr {
  static headers: Record<string, string> = {}
  status = 200
  responseText = '{"stage_id":"s"}'
  upload: Record<string, unknown> = {}
  onload: (() => void) | null = null
  onloadend: (() => void) | null = null
  onabort: (() => void) | null = null
  onerror: (() => void) | null = null
  open() {}
  abort() {}
  setRequestHeader(k: string, v: string) { FakeXhr.headers[k] = v }
  send() { queueMicrotask(() => this.onload?.()) }
}

afterEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  FakeXhr.headers = {}
  vi.unstubAllGlobals()
})

describe('adminRestoreFromUpload auth header', () => {
  it('omits Authorization instead of sending "Bearer null" without a token', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    await adminRestoreFromUpload(new File(['x'], 'b.tar.zst'))
    expect(FakeXhr.headers).not.toHaveProperty('Authorization')
  })

  it('sends the bearer token when one is stored', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 'tok')
    await adminRestoreFromUpload(new File(['x'], 'b.tar.zst'))
    expect(FakeXhr.headers.Authorization).toBe('Bearer tok')
  })
})


describe('adminApplyRestore ambiguity', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('reports a dropped connection as NetworkError (outcome unknown)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    const { adminApplyRestore } = await import('./backup')
    const { isNetworkError } = await import('./shared')
    const err = await adminApplyRestore('stage-1', 'pw').catch((e) => e)
    expect(isNetworkError(err)).toBe(true)
  })

  it('treats a gateway error during restart as NetworkError, but a wrong password as definite', async () => {
    const { adminApplyRestore } = await import('./backup')
    const { isNetworkError } = await import('./shared')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('bad gateway', { status: 502 })))
    expect(isNetworkError(await adminApplyRestore('s', 'pw').catch((e) => e))).toBe(true)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: 'password incorrect' }),
      { status: 401, headers: { 'content-type': 'application/json' } },
    )))
    const definite = await adminApplyRestore('s', 'pw').catch((e) => e)
    expect(isNetworkError(definite)).toBe(false)
    expect(definite.message).toBe('password incorrect')
  })
})

describe('XHR upload paths and a rejected staff token', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('maps a 401 "Invalid token" on the restore upload to UnauthorizedError, like fetch calls do', async () => {
    class Xhr401 {
      status = 401
      responseText = '{"detail":"Invalid token"}'
      upload: Record<string, unknown> = {}
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      onabort: (() => void) | null = null
      onloadend: (() => void) | null = null
      open() {}
      setRequestHeader() {}
      abort() {}
      send() { queueMicrotask(() => this.onload?.()) }
    }
    vi.stubGlobal('XMLHttpRequest', Xhr401)
    const { isUnauthorizedError } = await import('./shared')
    const err = await adminRestoreFromUpload(new File(['x'], 'b.tar.zst')).catch((e) => e)
    expect(isUnauthorizedError(err)).toBe(true)
  })

  it('reports a transport failure of the restore upload as NetworkError (localisable)', async () => {
    class XhrDown {
      upload: Record<string, unknown> = {}
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      onabort: (() => void) | null = null
      onloadend: (() => void) | null = null
      open() {}
      setRequestHeader() {}
      abort() {}
      send() { queueMicrotask(() => this.onerror?.()) }
    }
    vi.stubGlobal('XMLHttpRequest', XhrDown)
    const { isNetworkError } = await import('./shared')
    const err = await adminRestoreFromUpload(new File(['x'], 'b.tar.zst')).catch((e) => e)
    expect(isNetworkError(err)).toBe(true)
  })
})

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json' },
})
const html = (status: number) => new Response('<html>bad gateway</html>', {
  status, headers: { 'content-type': 'text/html' },
})

describe('which failures leave an apply / reset outcome unknown', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it("treats Cloudflare's 52x like nginx's gateway errors", async () => {
    const { adminApplyRestore, adminApplyReset } = await import('./backup')
    const { isNetworkError } = await import('./shared')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(html(524)))
    expect(isNetworkError(await adminApplyRestore('s', 'pw').catch((e) => e))).toBe(true)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(html(522)))
    expect(isNetworkError(await adminApplyReset('pw').catch((e) => e))).toBe(true)
  })

  it("treats only a gateway's 503 as ambiguous, not the app's own JSON 503", async () => {
    const { adminApplyRestore } = await import('./backup')
    const { isNetworkError } = await import('./shared')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(html(503)))
    expect(isNetworkError(await adminApplyRestore('s', 'pw').catch((e) => e))).toBe(true)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(503, { detail: 'the job could not be recorded' })))
    const definite = await adminApplyRestore('s', 'pw').catch((e) => e)
    expect(isNetworkError(definite)).toBe(false)
    expect(definite.message).toBe('the job could not be recorded')
  })

  it('maps a dropped reset request or a gateway error to NetworkError', async () => {
    const { adminApplyReset } = await import('./backup')
    const { isNetworkError } = await import('./shared')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    expect(isNetworkError(await adminApplyReset('pw').catch((e) => e))).toBe(true)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(html(504)))
    expect(isNetworkError(await adminApplyReset('pw').catch((e) => e))).toBe(true)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(401, { detail: 'password incorrect' })))
    const definite = await adminApplyReset('pw').catch((e) => e)
    expect(isNetworkError(definite)).toBe(false)
    expect(definite.message).toBe('password incorrect')
  })

  it('returns the reset job and its stage id', async () => {
    const { adminApplyReset } = await import('./backup')
    const started = { job_id: 'j', stage_id: 'stage-0123456789ab', scope: 'reset' }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(202, started)))
    expect(await adminApplyReset('pw')).toEqual(started)
  })
})

describe('restore apply refusals', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('carries the code and the server message of a 409 refusal', async () => {
    const { adminApplyRestore, isRestoreRefusedError } = await import('./backup')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(409, {
      detail: { code: 'ARCHIVE_INCONSISTENT', message: 'missing 1 wsi file(s)', integrity: {} },
    })))
    const err = await adminApplyRestore('s', 'pw').catch((e) => e)
    expect(isRestoreRefusedError(err)).toBe(true)
    expect(err.code).toBe('ARCHIVE_INCONSISTENT')
    expect(err.message).toBe('missing 1 wsi file(s)')
  })
})

describe('job polling', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('reports a job whose record is gone as JobNotFoundError', async () => {
    const { adminGetBackupJob, isJobNotFoundError } = await import('./backup')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(404, { detail: 'job not found' })))
    expect(isJobNotFoundError(await adminGetBackupJob('x').catch((e) => e))).toBe(true)
  })

  it('waitForJob rides out a blip and returns the terminal record', async () => {
    const { waitForJob } = await import('./backup')
    const done = { id: 'j', kind: 'restore_apply', state: 'succeeded', progress: {}, result: { mode: 'restart' } }
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(json(200, { ...done, state: 'running' }))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(json(200, done)))
    expect(await waitForJob('j', { intervalMs: 1 })).toEqual(done)
  })

  it('keepLogin: a 401 ends the wait without dropping the token or leaving the page', async () => {
    const { waitForJob } = await import('./backup')
    const { isUnauthorizedError, _resetLoginRedirectForTests } = await import('./shared')
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 'tok')
    const replace = vi.fn()
    const realLocation = window.location
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...realLocation, pathname: '/admin/backup', replace },
    })
    try {
      const running = { id: 'j', kind: 'restore_apply', state: 'running', progress: { phase: 'staging' }, result: {} }
      const seen: string[] = []
      vi.stubGlobal('fetch', vi.fn()
        .mockResolvedValueOnce(json(200, running))
        .mockResolvedValueOnce(json(401, { detail: 'Invalid token' })))
      const err = await waitForJob('j', { intervalMs: 1, keepLogin: true, onPoll: (j) => seen.push(j.state) })
        .catch((e) => e)
      expect(isUnauthorizedError(err)).toBe(true)
      expect(seen).toEqual(['running'])
      expect(sessionStorage.getItem(STORAGE_KEYS.adminToken)).toBe('tok')
      expect(replace).not.toHaveBeenCalled()
      // Without it, the rejected token sends the page back to the login.
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(401, { detail: 'Invalid token' })))
      expect(isUnauthorizedError(await waitForJob('j', { intervalMs: 1 }).catch((e) => e))).toBe(true)
      expect(replace).toHaveBeenCalledWith('/admin')
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: realLocation })
      _resetLoginRedirectForTests()
    }
  })

  it('waitForJob gives up on a record that is gone, at once', async () => {
    const { waitForJob, isJobNotFoundError } = await import('./backup')
    const fetchMock = vi.fn().mockResolvedValue(json(404, { detail: 'job not found' }))
    vi.stubGlobal('fetch', fetchMock)
    expect(isJobNotFoundError(await waitForJob('j', { intervalMs: 1 }).catch((e) => e))).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe('restore upload errors', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  function xhrAnswering(status: number, body: string) {
    return class {
      status = status
      responseText = body
      upload: Record<string, unknown> = {}
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      onabort: (() => void) | null = null
      onloadend: (() => void) | null = null
      open() {}
      setRequestHeader() {}
      abort() {}
      send() { queueMicrotask(() => this.onload?.()) }
    }
  }

  it('translates a coded 507 (no space) instead of showing raw JSON', async () => {
    vi.stubGlobal('XMLHttpRequest', xhrAnswering(507, '{"detail":{"code":"adminDash.apiErr.uploadNoSpace"}}'))
    const { isApiCodeError } = await import('./shared')
    const err = await adminRestoreFromUpload(new File(['x'], 'b.tar.zst')).catch((e) => e)
    expect(isApiCodeError(err)).toBe(true)
    expect(err.code).toBe('adminDash.apiErr.uploadNoSpace')
  })

  it('maps 413 and 429 like the fetch-based calls', async () => {
    const { isFileTooLargeError, isRateLimitError } = await import('./shared')
    vi.stubGlobal('XMLHttpRequest', xhrAnswering(413, '<html></html>'))
    expect(isFileTooLargeError(await adminRestoreFromUpload(new File(['x'], 'b')).catch((e) => e))).toBe(true)
    vi.stubGlobal('XMLHttpRequest', xhrAnswering(429, '<html></html>'))
    expect(isRateLimitError(await adminRestoreFromUpload(new File(['x'], 'b')).catch((e) => e))).toBe(true)
  })

  it('resolves to the staging job id', async () => {
    vi.stubGlobal('XMLHttpRequest', xhrAnswering(202, '{"job_id":"j-1"}'))
    expect(await adminRestoreFromUpload(new File(['x'], 'b.tar.zst'))).toEqual({ job_id: 'j-1' })
  })

  it("a proxy's own error page leaves the outcome unknown (restoreUploadGateway)", async () => {
    const { isApiCodeError } = await import('./shared')
    for (const status of [502, 504, 524]) {
      vi.stubGlobal('XMLHttpRequest', xhrAnswering(status, '<!DOCTYPE html><title>timeout</title>'))
      const err = await adminRestoreFromUpload(new File(['x'], 'b.tar.zst')).catch((e) => e)
      expect(isApiCodeError(err)).toBe(true)
      expect(err.code).toBe('adminDash.apiErr.restoreUploadGateway')
      expect(err.params).toEqual({ status })
    }
    // The app's own JSON answer is definite.
    vi.stubGlobal('XMLHttpRequest', xhrAnswering(503, '{"detail":"the job could not be recorded"}'))
    const own = await adminRestoreFromUpload(new File(['x'], 'b.tar.zst')).catch((e) => e)
    expect(isApiCodeError(own)).toBe(false)
    expect(own.message).toBe('the job could not be recorded')
  })
})

describe('a failed job', () => {
  it('stands for its translatable code when it has one, else its error text', async () => {
    const { backupJobError } = await import('./backup')
    const { isApiCodeError } = await import('./shared')
    const base = { id: 'j', kind: 'restore_stage' as const, state: 'failed' as const, created_at: '', progress: {} }
    const coded = backupJobError({ ...base, error: 'ENOSPC', result: { code: 'adminDash.apiErr.uploadNoSpace' } }, 'x')
    expect(isApiCodeError(coded) && coded.code).toBe('adminDash.apiErr.uploadNoSpace')
    const plain = backupJobError({ ...base, error: 'corrupt archive', result: {} }, 'x')
    expect(isApiCodeError(plain)).toBe(false)
    expect(plain.message).toBe('corrupt archive')
    expect(backupJobError({ ...base, error: null, result: {} }, 'fallback').message).toBe('fallback')
  })
})
