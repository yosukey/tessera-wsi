// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import React from 'react'
import { BrowserRouter } from 'react-router'

// main.tsx is the real entry point: stub only what needs a browser (React
// mounting, the LAN probe, the pages) and keep ErrorBoundary and its preload
// handler real, so the test exercises the wiring and nothing else.
const render = vi.hoisted(() => vi.fn())
// Records whether anything the entry imports pulls in OpenSeadragon.
const osdLoaded = vi.hoisted(() => ({ value: false }))
vi.mock('react-dom/client', () => ({ default: { createRoot: () => ({ render }) } }))
vi.mock('openseadragon', () => { osdLoaded.value = true; return { default: {} } })
vi.mock('szi-tile-source', () => { osdLoaded.value = true; return { enableSziTileSource: vi.fn() } })
vi.mock('./pages/App', () => ({ default: () => null }))
vi.mock('./lib/lanProbe', () => ({ runLanProbe: vi.fn() }))

// The first element of `type` in a rendered element tree.
function findElement(node: unknown, type: unknown): React.ReactElement<Record<string, unknown>> | null {
  if (!React.isValidElement(node)) return null
  if (node.type === type) return node as React.ReactElement<Record<string, unknown>>
  const children = React.Children.toArray((node.props as { children?: React.ReactNode }).children)
  for (const child of children) {
    const hit = findElement(child, type)
    if (hit) return hit
  }
  return null
}

describe('main.tsx entry', () => {
  beforeEach(() => {
    vi.resetModules()
    osdLoaded.value = false
    document.body.innerHTML = '<div id="root"></div>'
  })
  afterEach(() => render.mockReset())

  it('keeps OpenSeadragon out of the entry chunk', async () => {
    await import('./main')
    expect(render).toHaveBeenCalledTimes(1)
    expect(osdLoaded.value).toBe(false)
  })

  it('commits router location changes without transitions', async () => {
    await import('./main')
    const router = findElement(render.mock.calls[0][0], BrowserRouter)
    expect(router).not.toBeNull()
    expect(router!.props.useTransitions).toBe(false)
  })
})

describe('main.tsx stale-chunk wiring', () => {
  const reload = vi.fn()
  const realLocation = window.location
  // Every import of main.tsx registers a window listener; drop them between
  // tests so a previous test's copy of the handler doesn't also fire.
  let installed: EventListener[] = []

  beforeEach(() => {
    installed = []
    const realAdd = window.addEventListener.bind(window)
    vi.spyOn(window, 'addEventListener').mockImplementation((type: string, fn: any, opts?: any) => {
      if (type === 'vite:preloadError') installed.push(fn)
      realAdd(type, fn, opts)
    })
    vi.resetModules()
    sessionStorage.clear()
    window.name = ''
    document.body.innerHTML = '<div id="root"></div>'
    Object.defineProperty(window, 'location', { configurable: true, value: { ...realLocation, reload } })
  })

  afterEach(() => {
    for (const fn of installed) window.removeEventListener('vite:preloadError', fn)
    vi.restoreAllMocks()
    Object.defineProperty(window, 'location', { configurable: true, value: realLocation })
    reload.mockReset()
    render.mockReset()
  })

  it('reloads once for a vite:preloadError of a missing chunk, then leaves repeats to the fallback', async () => {
    await import('./main')
    expect(installed).toHaveLength(1)
    expect(render).toHaveBeenCalledTimes(1)

    const fire = () => {
      const ev = new Event('vite:preloadError', { cancelable: true })
      ;(ev as Event & { payload?: unknown }).payload = new Error('Unable to preload CSS for /assets/x.css')
      window.dispatchEvent(ev)
      return ev
    }
    const first = fire()
    expect(reload).toHaveBeenCalledTimes(1)
    expect(first.defaultPrevented).toBe(true) // the reload handles it

    // The reload did not fix it (the chunk is really gone): no second reload,
    // and the error is left unhandled so the boundary / console show it.
    const second = fire()
    expect(reload).toHaveBeenCalledTimes(1)
    expect(second.defaultPrevented).toBe(false)
  })

  it('does not schedule a timer that would clear the reload mark', async () => {
    vi.useFakeTimers()
    try {
      await import('./main')
      const ev = new Event('vite:preloadError', { cancelable: true })
      ;(ev as Event & { payload?: unknown }).payload = new Error('Failed to fetch dynamically imported module: /a.js')
      window.dispatchEvent(ev)
      expect(reload).toHaveBeenCalledTimes(1)
      // Long after boot - a timer forgetting the mark after 15 s would have
      // fired by now and allowed another reload.
      vi.advanceTimersByTime(60_000)
      const again = new Event('vite:preloadError', { cancelable: true })
      ;(again as Event & { payload?: unknown }).payload = new Error('Failed to fetch dynamically imported module: /a.js')
      window.dispatchEvent(again)
      expect(reload).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
