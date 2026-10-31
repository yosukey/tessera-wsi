// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { runLanProbe } from './lanProbe'

const realLocation = window.location
const replace = vi.fn()

beforeEach(() => {
  vi.stubEnv('VITE_LAN_HOST', 'lan.example.org')
  sessionStorage.clear()
  localStorage.clear()
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...realLocation, hostname: 'wsi.example.org', pathname: '/slides/x', search: '?a=1', hash: '', replace },
  })
})

afterEach(() => {
  vi.unstubAllEnvs()
  Object.defineProperty(window, 'location', { configurable: true, value: realLocation })
  replace.mockReset()
})

const settle = () => new Promise((r) => setTimeout(r, 0))

describe('runLanProbe', () => {
  it('probes with a bodiless no-cors HEAD request and hands off when the LAN host answers', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }))
    runLanProbe()
    await settle()
    expect(fetchSpy).toHaveBeenCalledTimes(1)
    const [url, init] = fetchSpy.mock.calls[0]
    expect(String(url)).toMatch(/^https:\/\/lan\.example\.org\/favicon\.ico\?t=\d+$/)
    expect(init).toMatchObject({ method: 'HEAD', mode: 'no-cors', cache: 'no-store' })
    expect(replace).toHaveBeenCalledWith('https://lan.example.org/slides/x?a=1')
    // The first-ever probe marks the device so later sessions take the fast path.
    expect(localStorage.getItem('wsi-lan-probed-once')).toBe('1')
  })

  it('does not move a visitor who has entered the dashboard while the probe was in flight', async () => {
    // The LAN origin has its own sessionStorage: the staff token, and an upload
    // or restore under way, would stay behind on this one.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }))
    runLanProbe()
    sessionStorage.setItem('admin_token', 'staff.jwt.token')
    await settle()
    expect(replace).not.toHaveBeenCalled()
  })

  it('does not move a visitor who is on an admin page either', async () => {
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...realLocation, hostname: 'wsi.example.org', pathname: '/admin/dashboard', search: '', hash: '', replace },
    })
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }))
    runLanProbe()
    await settle()
    expect(replace).not.toHaveBeenCalled()
  })

  it('retries the fast path a few times, then stays put', async () => {
    localStorage.setItem('wsi-lan-probed-once', '1')
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))
    runLanProbe()
    for (let i = 0; i < 5; i++) await settle()
    expect(fetchSpy).toHaveBeenCalledTimes(3)
    expect(replace).not.toHaveBeenCalled()
  })

  it('probes at most once per browser session', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'))
    runLanProbe()
    runLanProbe()
    await settle()
    expect(fetchSpy).toHaveBeenCalledTimes(1)
  })
})
