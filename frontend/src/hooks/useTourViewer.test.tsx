// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// A viewer double that records its handlers so a test can raise OSD's canvas
// events by hand, with an identity pixel -> image mapping (a click at (x, y)
// lands on image pixel (x, y)).
type FakeViewer = {
  element: HTMLDivElement
  fire: (name: string, ev: Record<string, unknown>) => void
  viewport: { setRotation: ReturnType<typeof vi.fn>; fitBounds: ReturnType<typeof vi.fn> }
}
const osd = vi.hoisted(() => ({ viewers: [] as FakeViewer[] }))
vi.mock('openseadragon', () => {
  class Point { constructor(public x: number, public y: number) {} }
  class Rect {
    constructor(public x: number, public y: number, public width: number, public height: number) {}
    rotate() { return this }
  }
  const OpenSeadragon = Object.assign(() => {
    const handlers: Record<string, Array<(ev: unknown) => void>> = {}
    const add = (name: string, fn: (ev: unknown) => void) => { (handlers[name] ||= []).push(fn) }
    const v = {
      element: document.createElement('div'),
      addHandler: add,
      addOnceHandler: add,
      removeHandler: () => {},
      destroy: () => {},
      addOverlay: () => {},
      removeOverlay: () => {},
      fire: (name: string, ev: Record<string, unknown>) => {
        for (const fn of handlers[name] || []) fn({ preventDefaultAction: false, ...ev })
      },
      viewport: {
        getRotation: () => 0,
        getBounds: () => new Rect(0, 0, 1, 1),
        setRotation: vi.fn(),
        fitBounds: vi.fn(),
        pointFromPixel: (p: Point) => p,
        viewportToImageCoordinates: (p: Point) => new Point(p.x, p.y),
        imageToViewportCoordinates: (p: Point) => p,
        imageToViewportRectangle: (r: Rect) => r,
      },
    }
    osd.viewers.push(v)
    return v
  }, { Point, Rect })
  return { default: OpenSeadragon }
})
const noFlip = vi.hoisted(() => ({ attached: [] as unknown[], detached: [] as unknown[] }))
vi.mock('../lib/osd', () => ({
  OSD_PREFIX_URL: '/osd-images/',
  osdMaxTilesPerFrame: () => 4,
  loadTileSource: async (id: string) => ({ ok: true, tileSource: `/dzi/${id}.dzi` }),
  attachZoomClamp: () => () => {},
  attachTwoFingerRotate: () => () => {},
  attachHomeResetsRotation: () => () => {},
  attachNoFlipKey: (v: unknown) => {
    noFlip.attached.push(v)
    return () => { noFlip.detached.push(v) }
  },
  attachMouseCoordsTracker: () => () => {},
  attachOpenFailed: () => () => {},
}))
vi.mock('../lib/api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../lib/api')>()
  return {
    ...real,
    getViewerSettings: vi.fn(async () => ({ ...real.DEFAULT_VIEWER_SETTINGS })),
    isStaffFromToken: () => false,
  }
})

import type { EditState, Step } from '../lib/tourEditHelpers'
import { useTourViewer } from './useTourViewer'

type HookArgs = Parameters<typeof useTourViewer>[0]
type HookResult = ReturnType<typeof useTourViewer>

const tour = { slide_id: 1 }
const slideObj = () => ({ id: 1, public_id: 's1', tile_version: 1 })
const stepObj = (id: number) => ({ id, order: id, center_x: 100, center_y: 200, fov_um_w: 50, fov_um_h: 40, rotation_deg: 0, pins: [], shapes_l0: [] }) as unknown as Step
const editStateFor = (over: Partial<EditState> = {}): EditState => ({
  label: '', description_md: '', rotation_deg: '0', fov_um_w: '50', fov_um_h: '40',
  center_x: '100', center_y: '200', pins: [], shapes_l0: [], transition_mode: 'smooth',
  ...over,
} as EditState)

let latest: HookResult
function Harness(props: HookArgs) {
  latest = useTourViewer(props)
  return <div ref={latest.viewerDivRef} />
}

const onAddPin = vi.fn()
const onAddShape = vi.fn()
function baseArgs(over: Partial<HookArgs> = {}): HookArgs {
  return {
    tour, slide: slideObj(), debugMode: false, selectedStep: null,
    editState: editStateFor(), editingStep: null, onAddPin, onAddShape,
    ...over,
  }
}

// Mount, wait for the viewer to be built, and let it "open".
async function mount(args: HookArgs) {
  const view = render(<Harness {...args} />)
  await waitFor(() => expect(osd.viewers).toHaveLength(1))
  const v = osd.viewers[0]
  act(() => v.fire('open', {}))
  return { v, rerender: (next: HookArgs) => view.rerender(<Harness {...next} />) }
}

beforeEach(() => {
  osd.viewers.length = 0
  noFlip.attached.length = 0
  noFlip.detached.length = 0
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('useTourViewer pin placement', () => {
  // OSD raises canvas-click on every release over the canvas — the end of a
  // drag-pan, the last finger lifting after a pinch — so a gesture is told
  // from a click by what happened since the press.
  function click(v: FakeViewer, x: number, y: number, quick = true) {
    v.fire('canvas-press', { position: { x, y } })
    v.fire('canvas-click', { position: { x, y }, quick })
  }

  it('places a pin on a click, not at the end of a pan or a pinch', async () => {
    const { v } = await mount(baseArgs())
    act(() => latest.setAddingPin(true))
    act(() => {
      v.fire('canvas-press', { position: { x: 0, y: 0 } })
      v.fire('canvas-drag', { position: { x: 20, y: 30 } })
      v.fire('canvas-drag', { position: { x: 30, y: 40 } })
      v.fire('canvas-click', { position: { x: 30, y: 40 }, quick: false })
    })
    // A short, fast flick still counts as a pan, whatever OSD's quick says.
    act(() => {
      v.fire('canvas-press', { position: { x: 0, y: 0 } })
      v.fire('canvas-drag', { position: { x: 30, y: 0 } })
      v.fire('canvas-click', { position: { x: 30, y: 0 }, quick: true })
    })
    act(() => {
      v.fire('canvas-press', { position: { x: 10, y: 10 } })
      v.fire('canvas-pinch', {})
      v.fire('canvas-click', { position: { x: 12, y: 12 }, quick: false })
    })
    expect(onAddPin).not.toHaveBeenCalled()
    // Still armed, so the click the user then makes drops the pin.
    expect(latest.addingPin).toBe(true)
    act(() => click(v, 10, 20))
    expect(onAddPin).toHaveBeenCalledTimes(1)
    expect(onAddPin).toHaveBeenCalledWith(10, 20)
    expect(latest.pendingPin).toEqual({ x: 10, y: 20 })
    expect(latest.addingPin).toBe(false)
  })

  it('places a pin on a slow click and on a press with a little jitter', async () => {
    const { v } = await mount(baseArgs())
    act(() => latest.setAddingPin(true))
    // Held past clickTimeThreshold: OSD says quick=false, but nothing moved.
    act(() => click(v, 10, 20, false))
    expect(onAddPin).toHaveBeenLastCalledWith(10, 20)
    act(() => latest.setAddingPin(true))
    // A pen / finger that wobbles a few pixels (past OSD's 5 px
    // clickDistThreshold) is still a press in place.
    act(() => {
      v.fire('canvas-press', { position: { x: 50, y: 50 } })
      v.fire('canvas-drag', { position: { x: 54, y: 53 } })
      v.fire('canvas-drag', { position: { x: 57, y: 51 } })
      v.fire('canvas-click', { position: { x: 57, y: 51 }, quick: false })
    })
    expect(onAddPin).toHaveBeenCalledTimes(2)
    expect(onAddPin).toHaveBeenLastCalledWith(57, 51)
  })

  it('places no pin from a two-finger tap', async () => {
    const { v } = await mount(baseArgs())
    act(() => latest.setAddingPin(true))
    // The second finger lands (a non-primary pointer) and both lift before
    // either moved enough for OSD to report a pinch.
    act(() => {
      v.element.dispatchEvent(Object.assign(new Event('pointerdown'), { isPrimary: true, pointerType: 'touch' }))
      v.fire('canvas-press', { position: { x: 10, y: 10 } })
      v.element.dispatchEvent(Object.assign(new Event('pointerdown'), { isPrimary: false, pointerType: 'touch' }))
      v.fire('canvas-click', { position: { x: 10, y: 10 }, quick: true })
    })
    expect(onAddPin).not.toHaveBeenCalled()
    expect(latest.addingPin).toBe(true)
    act(() => click(v, 10, 20))
    expect(onAddPin).toHaveBeenCalledWith(10, 20)
  })

  it('ignores clicks while not in pin mode', async () => {
    const { v } = await mount(baseArgs())
    act(() => click(v, 10, 20))
    expect(onAddPin).not.toHaveBeenCalled()
  })
})

describe('useTourViewer shape drawing', () => {
  it('commits a dragged rectangle', async () => {
    const { v } = await mount(baseArgs())
    act(() => latest.setAddingShape('rect'))
    act(() => {
      v.fire('canvas-press', { position: { x: 10, y: 20 } })
      v.fire('canvas-drag', { position: { x: 40, y: 60 } })
      v.fire('canvas-drag-end', { position: { x: 50, y: 70 } })
    })
    expect(onAddShape).toHaveBeenCalledTimes(1)
    expect(onAddShape.mock.calls[0][0]).toMatchObject({ kind: 'rect', x: 10, y: 20, w: 40, h: 50 })
    expect(latest.addingShape).toBeNull()
  })

  it.each(['rect', 'circle', 'freehand'] as const)('draws no %s from a pinch', async (kind) => {
    const { v } = await mount(baseArgs())
    act(() => latest.setAddingShape(kind))
    act(() => {
      // The first finger lands and moves a little before the second one does
      // (fingers rarely land in the same frame), so OSD has already reported
      // a one-finger drag; the pinch then zooms, and lifting the fingers ends
      // the "drag".
      v.fire('canvas-press', { position: { x: 10, y: 20 } })
      v.fire('canvas-drag', { position: { x: 13, y: 22 } })
      v.fire('canvas-drag', { position: { x: 16, y: 25 } })
      v.fire('canvas-pinch', {})
      v.fire('canvas-drag-end', { position: { x: 200, y: 300 } })
    })
    expect(onAddShape).not.toHaveBeenCalled()
    expect(latest.draftShape).toBeNull()
    // The mode stays armed for the drag the user meant to draw.
    expect(latest.addingShape).toBe(kind)
    act(() => {
      v.fire('canvas-press', { position: { x: 0, y: 0 } })
      v.fire('canvas-drag', { position: { x: 20, y: 20 } })
      v.fire('canvas-drag', { position: { x: 40, y: 40 } })
      v.fire('canvas-drag-end', { position: { x: 40, y: 40 } })
    })
    expect(onAddShape).toHaveBeenCalledTimes(1)
    expect(onAddShape.mock.calls[0][0]).toMatchObject({ kind })
  })
})

describe('useTourViewer edited-step framing', () => {
  // Mount with no step open, then open step 3 the way the editor does
  // (openEditor resets the applied-view memo before swapping the state in).
  async function openStep3() {
    const args = baseArgs()
    const m = await mount(args)
    const open = { ...args, editingStep: stepObj(3), editState: editStateFor() }
    act(() => latest.resetLastAppliedView())
    m.rerender(open)
    expect(m.v.viewport.fitBounds).toHaveBeenCalledTimes(1)
    return { ...m, open }
  }

  it('frames the step when it is opened, and not again on unrelated re-renders', async () => {
    const { v, rerender, open } = await openStep3()
    // The user pans away, then something hands the editor fresh copies of
    // the same slide and step: "Save as preset" (setSlide({...prev})), a
    // refresh(), or ↑/↓ / delete renumbering the steps (applyStepOrder).
    rerender({ ...open, slide: slideObj(), editingStep: stepObj(3), editState: { ...open.editState } })
    rerender({ ...open, slide: slideObj(), editingStep: { ...stepObj(3), order: 2 } as Step })
    expect(v.viewport.fitBounds).toHaveBeenCalledTimes(1)
  })

  it('re-frames when the edited view changes or another step is opened', async () => {
    const { v, rerender, open } = await openStep3()
    // Typing a new FOV re-frames live.
    const typed = { ...open, editState: editStateFor({ fov_um_w: '80' }) }
    rerender(typed)
    expect(v.viewport.fitBounds).toHaveBeenCalledTimes(2)
    // Opening another step re-frames, even onto the very same view (a
    // duplicated step): openEditor clears the memo first.
    act(() => latest.resetLastAppliedView())
    rerender({ ...typed, editingStep: stepObj(4) })
    expect(v.viewport.fitBounds).toHaveBeenCalledTimes(3)
  })

  it('re-frames a value that was cleared and then typed again', async () => {
    const { v, rerender, open } = await openStep3()
    // The user pans away, clears the FOV field (nothing to frame yet), then
    // retypes the same value: that is a request to frame it again.
    rerender({ ...open, editState: editStateFor({ fov_um_w: '' }) })
    expect(v.viewport.fitBounds).toHaveBeenCalledTimes(1)
    rerender({ ...open, editState: editStateFor() })
    expect(v.viewport.fitBounds).toHaveBeenCalledTimes(2)
    // Likewise through an invalid (non-numeric / non-positive) value.
    rerender({ ...open, editState: editStateFor({ fov_um_h: '0' }) })
    rerender({ ...open, editState: editStateFor() })
    expect(v.viewport.fitBounds).toHaveBeenCalledTimes(3)
  })

  it("re-frames when the slide's pixel size changes under the same fields", async () => {
    const { v, rerender, open } = await openStep3()
    // A refresh() after the slide's MPP was set or corrected: the field
    // strings are identical, but they now mean a different field of view.
    rerender({ ...open, slide: { ...slideObj(), mpp_x: 0.25, mpp_y: 0.25 } })
    expect(v.viewport.fitBounds).toHaveBeenCalledTimes(2)
    // Only mpp_y changing leaves stepMpp alone but not the physical -> stored
    // conversion of an anisotropic slide.
    rerender({ ...open, slide: { ...slideObj(), mpp_x: 0.25, mpp_y: 0.5 } })
    expect(v.viewport.fitBounds).toHaveBeenCalledTimes(3)
    rerender({ ...open, slide: { ...slideObj(), mpp_x: 0.25, mpp_y: 0.5 } })
    expect(v.viewport.fitBounds).toHaveBeenCalledTimes(3)
  })

  it('leaves the viewport alone when the current view is applied to the step', async () => {
    const { v, rerender, open } = await openStep3()
    const captured = { rotation_deg: '12', fov_um_w: '70', fov_um_h: '60', center_x: '300', center_y: '400' }
    act(() => latest.rememberAppliedView(captured))
    rerender({ ...open, editState: editStateFor(captured) })
    expect(v.viewport.fitBounds).toHaveBeenCalledTimes(1)
  })
})

describe('useTourViewer keeps the slide from being mirrored', () => {
  it("turns OpenSeadragon's f key off on the editor's viewer", async () => {
    const { v } = await mount(baseArgs())
    expect(noFlip.attached).toEqual([v])
    cleanup()
    expect(noFlip.detached).toEqual([v])
  })
})
