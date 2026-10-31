// @vitest-environment jsdom
import React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { I18nProvider } from '../lib/i18n'
import { STORAGE_KEYS } from '../lib/storageKeys'
import ErrorBoundary, {
  handlePreloadError, isChunkLoadError, RELOAD_COOLDOWN_MS, reloadOnceForStaleChunk,
} from './ErrorBoundary'

afterEach(() => {
  cleanup()
  sessionStorage.clear()
  window.name = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
})

function Boom({ message }: { message: string }): React.ReactElement {
  throw new Error(message)
}

describe('isChunkLoadError', () => {
  it('recognises the stale-chunk messages of the major browsers', () => {
    expect(isChunkLoadError(new Error('Failed to fetch dynamically imported module: /a.js'))).toBe(true)
    expect(isChunkLoadError(new Error('error loading dynamically imported module'))).toBe(true)
    expect(isChunkLoadError(new Error('Importing a module script failed.'))).toBe(true)
    expect(isChunkLoadError(new Error('x is not a function'))).toBe(false)
  })
})

describe('ErrorBoundary', () => {
  it('shows a recoverable fallback instead of a blank page', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <I18nProvider>
        <ErrorBoundary><Boom message="render failure" /></ErrorBoundary>
      </I18nProvider>,
    )
    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.getByRole('button')).toBeTruthy()
  })

  it('does not loop when sessionStorage is blocked', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const reload = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { ...window.location, reload } })
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    window.name = ''
    const mount = () => render(
      <I18nProvider>
        <ErrorBoundary><Boom message="Failed to fetch dynamically imported module: /a.js" /></ErrorBoundary>
      </I18nProvider>,
    )
    mount()
    cleanup()
    mount()
    expect(reload).toHaveBeenCalledTimes(1)
    window.name = ''
  })

  it('reloads once for a stale chunk, then not again', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const reload = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { ...window.location, reload } })
    const mount = () => render(
      <I18nProvider>
        <ErrorBoundary><Boom message="Failed to fetch dynamically imported module: /a.js" /></ErrorBoundary>
      </I18nProvider>,
    )
    mount()
    expect(reload).toHaveBeenCalledTimes(1)
    cleanup()
    mount()
    expect(reload).toHaveBeenCalledTimes(1)
  })
})


describe('handlePreloadError', () => {
  function preloadEvent(payload: unknown): Event {
    const ev = new Event('vite:preloadError', { cancelable: true })
    ;(ev as Event & { payload?: unknown }).payload = payload
    return ev
  }

  it('reloads once for a missing chunk and swallows the event', () => {
    const reload = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { ...window.location, reload } })
    window.name = ''
    const ev = preloadEvent(new Error('Failed to fetch dynamically imported module: /a.js'))
    handlePreloadError(ev)
    expect(ev.defaultPrevented).toBe(true)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('leaves any other failure alone (no reload, event not swallowed)', () => {
    const reload = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { ...window.location, reload } })
    vi.spyOn(console, 'error').mockImplementation(() => {})
    window.name = ''
    const ev = preloadEvent(new Error('BOOM thrown while evaluating the chunk'))
    handlePreloadError(ev)
    expect(ev.defaultPrevented).toBe(false)
    expect(reload).not.toHaveBeenCalled()
  })
})


describe('reload cooldown', () => {
  function stubReload() {
    const reload = vi.fn()
    Object.defineProperty(window, 'location', { configurable: true, value: { ...window.location, reload } })
    return reload
  }

  it('does not loop however late after page load the chunk failure happens', () => {
    // A mark cleared 15 s after boot would let a chunk that failed later
    // than that (a gateway timeout) reload forever.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const reload = stubReload()
    expect(reloadOnceForStaleChunk()).toBe(true)
    for (const seconds of [16, 40, 90, 200]) {
      vi.setSystemTime(new Date(Date.parse('2026-01-01T00:00:00Z') + seconds * 1000))
      expect(reloadOnceForStaleChunk()).toBe(false)
    }
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('allows another reload for a genuinely later redeploy (past the cooldown)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    const reload = stubReload()
    expect(reloadOnceForStaleChunk()).toBe(true)
    vi.setSystemTime(new Date(Date.parse('2026-01-01T00:00:00Z') + RELOAD_COOLDOWN_MS + 1000))
    expect(reloadOnceForStaleChunk()).toBe(true)
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('also holds with sessionStorage blocked (window.name carries the time)', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    const reload = stubReload()
    expect(reloadOnceForStaleChunk()).toBe(true)
    vi.setSystemTime(new Date(Date.parse('2026-01-01T00:00:00Z') + 20_000))
    expect(reloadOnceForStaleChunk()).toBe(false)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('with sessionStorage blocked, window.name also lets a later redeploy reload after the cooldown', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    const reload = stubReload()
    expect(reloadOnceForStaleChunk()).toBe(true)
    vi.setSystemTime(new Date(Date.parse('2026-01-01T00:00:00Z') + RELOAD_COOLDOWN_MS + 1000))
    expect(reloadOnceForStaleChunk()).toBe(true)
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('falls back to window.name when storage refuses the write but still holds an older mark', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
    sessionStorage.setItem(STORAGE_KEYS.chunkReload, '1') // a stale value, as an earlier page load leaves
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
    const reload = stubReload()
    expect(reloadOnceForStaleChunk()).toBe(true)
    vi.setSystemTime(new Date(Date.parse('2026-01-01T00:00:00Z') + 20_000))
    expect(reloadOnceForStaleChunk()).toBe(false) // no reload loop
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('leaves the preload event alone when the cooldown refused the reload', () => {
    const reload = stubReload()
    reloadOnceForStaleChunk() // uses up the cooldown
    const ev = new Event('vite:preloadError', { cancelable: true })
    ;(ev as Event & { payload?: unknown }).payload = new Error('Failed to fetch dynamically imported module: /a.js')
    handlePreloadError(ev)
    expect(ev.defaultPrevented).toBe(false)
    expect(reload).toHaveBeenCalledTimes(1)
  })
})
