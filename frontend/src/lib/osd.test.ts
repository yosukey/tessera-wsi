// @vitest-environment jsdom
import OpenSeadragon from 'openseadragon'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { attachNoFlipKey, attachOpenFailed, loadTileSource, osdMaxTilesPerFrame } from './osd'
import { dziTileUrl } from './tileUrl'

// What GET /dzi/{public_id}.dzi returns (OpenSlide's DeepZoomGenerator).
const DESCRIPTOR =
  '<?xml version="1.0" encoding="UTF-8"?>' +
  '<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" Format="jpeg" Overlap="1" TileSize="256">' +
  '<Size Height="3000" Width="4000"/></Image>'

type Osd = typeof OpenSeadragon & {
  SziTileSource?: unknown
  makeAjaxRequest: (opts: { url: string; success: (xhr: unknown) => void }) => void
}
const osd = OpenSeadragon as Osd

afterEach(() => {
  delete osd.SziTileSource
})

describe('lib/osd registers the SZI tile source', () => {
  it('on import, so loadTileSource can open an SZI slide without other wiring', async () => {
    delete osd.SziTileSource
    vi.resetModules()
    const fresh = await import('./osd')
    expect(typeof (osd.SziTileSource as { createSziTileSource?: unknown } | undefined)?.createSziTileSource).toBe('function')
    // The real factory is what loadTileSource reaches (it fetches the archive).
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'))
    const r = await fresh.loadTileSource('abc', true, undefined, 1)
    expect(r).toMatchObject({ ok: false, reason: 'szi-fetch-failed' })
    expect(fetchSpy).toHaveBeenCalled()
    expect(String(fetchSpy.mock.calls[0][0])).toBe('/dzi/abc.szi?v=1')
  })
})

describe('loadTileSource: tile URLs carry the WSI generation', () => {
  it('versions the DZI descriptor URL with tile_version', async () => {
    const r = await loadTileSource('abc_D-1', false, undefined, 3)
    expect(r).toEqual({ ok: true, tileSource: '/dzi/abc_D-1.dzi?v=3' })
  })

  it('falls back to the unversioned URL when the API reports no version', async () => {
    // e.g. a slide object from an endpoint that doesn't carry tile_version:
    // still viewable, the server just never edge-caches those tiles.
    for (const v of [undefined, null]) {
      const r = await loadTileSource('abc', false, undefined, v)
      expect(r).toEqual({ ok: true, tileSource: '/dzi/abc.dzi' })
    }
  })

  it('OpenSeadragon carries ?v= from the descriptor URL onto every tile URL', async () => {
    // The server-side design relies on this (nginx keys tiles on $uri?v=$arg_v):
    // run the descriptor through the same TileSource -> getImageInfo ->
    // DziTileSource.configure path the Viewer takes for a string tileSources.
    const r = await loadTileSource('abc_D-1', false, undefined, 7)
    if (!r.ok) throw new Error('expected a tile source')
    const requested: string[] = []
    vi.spyOn(osd, 'makeAjaxRequest').mockImplementation((opts) => {
      requested.push(opts.url)
      opts.success({ responseText: DESCRIPTOR, status: 200 })
    })
    const ready = await new Promise<OpenSeadragon.TileSource>((resolve, reject) => {
      const ts = new OpenSeadragon.TileSource({ url: r.tileSource } as unknown as OpenSeadragon.TileSourceOptions)
      // The bundled typings declare the event's tileSource as `object`.
      ts.addHandler('ready', (e) => resolve(e.tileSource as OpenSeadragon.TileSource))
      ts.addHandler('open-failed', (e) => reject(new Error(String(e.message))))
    })
    expect(requested).toEqual(['/dzi/abc_D-1.dzi?v=7'])
    expect(ready).toBeInstanceOf(OpenSeadragon.DziTileSource)
    expect(String(ready.getTileUrl(12, 3, 4))).toBe('/dzi/abc_D-1_files/12/3_4.jpeg?v=7')
    expect(String(ready.getTileUrl(0, 0, 0))).toBe('/dzi/abc_D-1_files/0/0_0.jpeg?v=7')
    // Code that fetches tiles itself (the admin browser perf test) spells them
    // identically, so it hits and warms the same cache entries viewers use.
    expect(dziTileUrl('abc_D-1', 12, 3, 4, 'jpeg', 7)).toBe(String(ready.getTileUrl(12, 3, 4)))
  })

  it('versions the SZI archive URL every Range read goes to', async () => {
    const createSziTileSource = vi.fn(async (url: string, fetchOptions?: unknown) => ({ url, fetchOptions }))
    osd.SziTileSource = { createSziTileSource }
    const headers = { Authorization: 'Bearer staff.jwt' }
    const r = await loadTileSource('abc', true, headers, 5)
    expect(r.ok).toBe(true)
    expect(createSziTileSource).toHaveBeenCalledWith('/dzi/abc.szi?v=5', { headers })
  })
})

describe('attachOpenFailed', () => {
  it("reports a failed open and takes down OSD's raw English message", async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('Not found', { status: 404 }))
    // jsdom has no matchMedia; the Viewer constructor probes it.
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: false, media: query,
      addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
    }))
    const el = document.createElement('div')
    document.body.appendChild(el)
    const viewer = OpenSeadragon({ element: el, prefixUrl: '/osd-images/' })
    const onFail = vi.fn()
    const detach = attachOpenFailed(viewer, onFail)
    const failed = new Promise<void>((resolve) => viewer.addHandler('open-failed', () => resolve()))
    viewer.open('/dzi/missing.dzi?v=3' as unknown as OpenSeadragon.TileSourceSpecifier)
    await failed
    expect(onFail).toHaveBeenCalledTimes(1)
    // OSD's own handler would put "Unable to open … /dzi/missing.dzi?v=3" here.
    expect(el.querySelector('.openseadragon-message')).toBeNull()
    expect(el.textContent).not.toContain('/dzi/')

    detach()
    viewer.destroy()
    el.remove()
    vi.unstubAllGlobals()
  })
})

describe('osdMaxTilesPerFrame', () => {
  it('follows the pixel ratio squared, from OSD\'s 1 up to 4', () => {
    // A screen's tile count scales with the pixel ratio squared.
    expect([1, 1.25, 1.5, 2, 3].map((r) => osdMaxTilesPerFrame(r))).toEqual([1, 2, 2, 4, 4])
    expect(osdMaxTilesPerFrame(0.5)).toBe(1)
  })
})

describe('attachNoFlipKey', () => {
  function realViewer() {
    const element = document.createElement('div')
    document.body.appendChild(element)
    const v = OpenSeadragon({ element, drawer: 'html' } as OpenSeadragon.Options)
    return { v, done: () => { v.destroy(); element.remove() } }
  }
  const press = (v: OpenSeadragon.Viewer, keyCode: number, code: string) =>
    v.canvas.dispatchEvent(new KeyboardEvent('keydown', { keyCode, code, bubbles: true } as KeyboardEventInit))

  it("stops OpenSeadragon's f key from mirroring the slide", () => {
    const { v, done } = realViewer()
    try {
      // Without it, the key flips the view (the behaviour being turned off).
      press(v, 70, 'KeyF')
      expect(v.viewport.getFlip()).toBe(true)
      v.viewport.setFlip(false)

      const detach = attachNoFlipKey(v)
      press(v, 70, 'KeyF')
      expect(v.viewport.getFlip()).toBe(false)

      detach()
      press(v, 70, 'KeyF')
      expect(v.viewport.getFlip()).toBe(true)
    } finally {
      done()
    }
  })

  it('leaves every other key to OpenSeadragon', () => {
    const { v, done } = realViewer()
    try {
      attachNoFlipKey(v)
      const prevented: Record<string, boolean> = {}
      // Registered after the helper, so it sees the helper's verdict.
      v.addHandler('canvas-key', (ev) => {
        prevented[(ev.originalEvent as KeyboardEvent).code] = ev.preventDefaultAction
      })
      press(v, 82, 'KeyR')
      press(v, 48, 'Digit0')
      press(v, 70, 'KeyF')
      expect(prevented).toEqual({ KeyR: false, Digit0: false, KeyF: true })
    } finally {
      done()
    }
  })
})
