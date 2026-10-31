// @vitest-environment jsdom
import React from 'react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The API module is the editor's only I/O; the OpenSeadragon-backed viewer
// hook and the assets panel are stubbed so the test sees the step list and
// the revision each structural write expects.
const api = vi.hoisted(() => ({
  adminGetTour: vi.fn(),
  adminGetSlide: vi.fn(),
  adminListTourAssets: vi.fn(),
  adminAddStep: vi.fn(),
  adminReorderSteps: vi.fn(),
  adminUpdateStep: vi.fn(),
  adminUpdateTour: vi.fn(),
  adminDeleteStep: vi.fn(),
  adminDeleteTour: vi.fn(),
  isManagerOrAboveFromToken: vi.fn(),
}))
// The viewer's pending pin, settable per test.
const pin = vi.hoisted(() => ({ current: null as null | { x: number; y: number }, set: vi.fn() }))
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return { ...actual, ...api }
})
vi.mock('../hooks/useTourViewer', () => {
  const noop = () => {}
  const viewer = {
    viewerDivRef: { current: null },
    scaleBarDivRef: { current: null },
    loadError: null,
    mouseCoords: null,
    get pendingPin() { return pin.current },
    setPendingPin: (...a: unknown[]) => pin.set(...a),
    addingPin: false,
    setAddingPin: noop,
    addingShape: null,
    setAddingShape: noop,
    cancelDraft: noop,
    jumpToStep: noop,
    rememberAppliedView: noop,
    resetLastAppliedView: noop,
    captureCurrentView: () => ({ centerX: 50, centerY: 50, fovUmW: 100, fovUmH: 100, rot: 0 }),
  }
  return { useTourViewer: () => viewer }
})
vi.mock('../components/admin/TourAssetsPanel', () => ({ default: () => null }))

import { I18nProvider } from '../lib/i18n'
import { RateLimitError } from '../lib/api'
import type { Slide, TourDetail, TourStep } from '../lib/api'
import AdminTourEditor from './AdminTourEditor'

function step(id: number, order: number): TourStep {
  return {
    id, order, label: `Step ${id}`, center_x: 10, center_y: 10, fov_um_w: 100, fov_um_h: 100,
    rotation_deg: 0, pins: [], shapes_l0: [], description_md: '', transition_mode: 'smooth', revision: 1,
  }
}

function detail(revision: number, steps: TourStep[]): TourDetail {
  return {
    tour: {
      id: 7, public_id: 't7', slide_public_id: 's1', slide_id: 1, title: 'Tour', description: '',
      is_published: false, unlisted: false, revision,
    },
    steps,
  }
}

const SLIDE = {
  id: 1, public_id: 's1', catalog_id: '', title: 'Slide', thumb_url: '', width: 1000, height: 1000,
  mpp_x: 0.25, mpp_y: 0.25, vendor: '', wsi_filename: 's1.svs', attributes: {}, is_published: false,
} as unknown as Slide

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/tours/7']}>
      <I18nProvider>
        <Routes>
          <Route path="/admin/tours/:tourId" element={<AdminTourEditor />} />
        </Routes>
      </I18nProvider>
    </MemoryRouter>,
  )
}

const RELOAD_FAILED = /The change was saved, but reloading the tour failed/
const upButtons = () => screen.getAllByRole('button', { name: '↑' })

beforeEach(() => {
  api.adminGetSlide.mockResolvedValue(SLIDE)
  api.adminListTourAssets.mockResolvedValue({ items: [] })
  api.adminReorderSteps.mockResolvedValue({ ok: true, count: 2, revision: 99 })
  api.isManagerOrAboveFromToken.mockReturnValue(true)
})

afterEach(() => {
  pin.current = null
  cleanup()
  vi.resetAllMocks()
})

describe('AdminTourEditor step add / duplicate and the tour revision', () => {
  // The new step reaches the list only through the reload. Until it does, the
  // editor must keep expecting the revision its list was loaded at, so the
  // next structural write is refused with a tour conflict (which resyncs)
  // rather than sending a step order that is missing the new step.
  it('keeps the loaded revision when the reload after a duplicate fails', async () => {
    api.adminGetTour
      .mockResolvedValueOnce(detail(5, [step(1, 1), step(2, 2)]))
      .mockRejectedValue(new RateLimitError())
    api.adminAddStep.mockResolvedValue({ id: 3, revision: 6 })
    mount()
    await screen.findByText('2. Step 2')
    fireEvent.click(screen.getAllByRole('button', { name: 'Duplicate' })[1])
    expect(await screen.findByText(RELOAD_FAILED)).toBeTruthy()
    expect(api.adminAddStep).toHaveBeenCalledWith(7, expect.anything(), 5)

    fireEvent.click(upButtons()[1])
    await waitFor(() => expect(api.adminReorderSteps).toHaveBeenCalledTimes(1))
    expect(api.adminReorderSteps).toHaveBeenCalledWith(7, [2, 1], 5)
  })

  it('keeps the loaded revision when the reload after an added step fails', async () => {
    api.adminGetTour
      .mockResolvedValueOnce(detail(5, [step(1, 1), step(2, 2)]))
      .mockRejectedValue(new RateLimitError())
    api.adminAddStep.mockResolvedValue({ id: 3, revision: 6 })
    mount()
    await screen.findByText('2. Step 2')
    fireEvent.click(screen.getByRole('button', { name: 'Add Step from Current View' }))
    expect(await screen.findByText(RELOAD_FAILED)).toBeTruthy()

    fireEvent.click(upButtons()[1])
    await waitFor(() => expect(api.adminReorderSteps).toHaveBeenCalledTimes(1))
    expect(api.adminReorderSteps).toHaveBeenCalledWith(7, [2, 1], 5)
  })

  it('expects the reloaded revision once the reload has brought the new step in', async () => {
    api.adminGetTour
      .mockResolvedValueOnce(detail(5, [step(1, 1), step(2, 2)]))
      .mockResolvedValue(detail(6, [step(1, 1), step(2, 2), step(3, 3)]))
    api.adminAddStep.mockResolvedValue({ id: 3, revision: 6 })
    mount()
    await screen.findByText('2. Step 2')
    fireEvent.click(screen.getAllByRole('button', { name: 'Duplicate' })[1])
    expect(await screen.findByText('Step duplicated')).toBeTruthy()
    expect(screen.getByText('3. Step 3')).toBeTruthy()

    fireEvent.click(upButtons()[2])
    await waitFor(() => expect(api.adminReorderSteps).toHaveBeenCalledTimes(1))
    expect(api.adminReorderSteps).toHaveBeenCalledWith(7, [1, 3, 2], 6)
  })
})

describe('AdminTourEditor duplicate of a step at its point budget', () => {
  it('sends every vertex in the one create request (which the server sizes its form for)', async () => {
    // 50,000 vertices (the per-step budget) in shapes of at most 4096.
    const shapes = Array.from({ length: 13 }, (_, i) => ({
      id: `s${i}`, kind: 'freehand' as const, x: 0, y: 0, w: 10, h: 10,
      stroke_color: 'red', fill_enabled: false, fill_opacity: 0.2, smoothing: true,
      label: '', label_position: 'above' as const, z: i,
      points: Array.from({ length: i < 12 ? 4000 : 2000 }, (_, j) => ({ x: j + 0.123456789, y: i + 0.987654321 })),
    }))
    const big = { ...step(2, 2), shapes_l0: shapes }
    api.adminGetTour.mockResolvedValue(detail(5, [step(1, 1), big]))
    api.adminAddStep.mockResolvedValue({ id: 3, revision: 6 })
    mount()
    await screen.findByText('2. Step 2')
    fireEvent.click(screen.getAllByRole('button', { name: 'Duplicate' })[1])
    await waitFor(() => expect(api.adminAddStep).toHaveBeenCalledTimes(1))
    const sent = JSON.parse(api.adminAddStep.mock.calls[0][1].shapes_l0) as { points: unknown[] }[]
    expect(sent.reduce((n, s) => n + s.points.length, 0)).toBe(50_000)
  })
})

describe('AdminTourEditor step label', () => {
  it('refuses to save a step without a label, and saves once one is given', async () => {
    // A step stored without a label.
    const unlabeled = { ...step(2, 2), label: '' }
    api.adminGetTour.mockResolvedValue(detail(5, [step(1, 1), unlabeled]))
    api.adminUpdateStep.mockResolvedValue({ step: { ...unlabeled, label: 'Overview', revision: 2 }, revision: 2 })
    mount()
    await screen.findByText('1. Step 1')
    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[1])

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findAllByText('A label is required.')).toHaveLength(2)
    expect(api.adminUpdateStep).not.toHaveBeenCalled()

    fireEvent.change(screen.getByRole('textbox', { name: 'Label' }), { target: { value: 'Overview' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.adminUpdateStep).toHaveBeenCalledTimes(1))
    expect(api.adminUpdateStep.mock.calls[0][2]).toMatchObject({ label: 'Overview' })
  })
})

describe('AdminTourEditor tour title', () => {
  it('holds the title still while its save is in flight, and sends it once', async () => {
    api.adminGetTour.mockResolvedValue(detail(5, [step(1, 1)]))
    let finish!: (v: unknown) => void
    api.adminUpdateTour.mockReturnValue(new Promise((resolve) => { finish = resolve }))
    mount()
    await screen.findByText('1. Step 1')
    fireEvent.click(screen.getByRole('button', { name: 'Edit title' }))
    const input = screen.getByDisplayValue('Tour') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Renamed' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => expect(input.readOnly).toBe(true))
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveProperty('disabled', true)
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.getByDisplayValue('Renamed')).toBe(input)

    const saved = detail(6, [step(1, 1)]).tour
    finish({ tour: { ...saved, title: 'Renamed' } })
    expect(await screen.findByText('Renamed')).toBeTruthy()
    expect(api.adminUpdateTour).toHaveBeenCalledTimes(1)
    expect(api.adminUpdateTour.mock.calls[0][1]).toEqual({ title: 'Renamed' })
  })
})

describe('AdminTourEditor writes that carry the tour revision', () => {
  it('holds a step delete and the visibility while a reorder is out', async () => {
    // Each sends the tour revision: two out at once would refuse the second
    // as "changed elsewhere".
    api.adminGetTour.mockResolvedValue(detail(5, [step(1, 1), step(2, 2)]))
    api.adminReorderSteps.mockReturnValue(new Promise(() => {}))
    mount()
    await screen.findByText('2. Step 2')
    fireEvent.click(upButtons()[1])
    await waitFor(() => expect(api.adminReorderSteps).toHaveBeenCalledTimes(1))
    const deletes = screen.getAllByRole('button', { name: 'Delete' }) as HTMLButtonElement[]
    expect(deletes.every((b) => b.disabled)).toBe(true)
    expect((screen.getByRole('button', { name: 'Add Step from Current View' }) as HTMLButtonElement).disabled).toBe(true)
    expect(api.adminDeleteStep).not.toHaveBeenCalled()
    // The visibility, the tour's metadata (its save sends the tour revision
    // too) and Delete tour wait as well.
    expect((screen.getByRole('button', { name: 'Open' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: /^Metadata/ }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Delete tour' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it("keeps a pin placed and a step picked while an add / duplicate is out", async () => {
    api.adminGetTour.mockResolvedValue(detail(5, [step(1, 1), step(2, 2)]))
    let finish: (v: unknown) => void = () => {}
    api.adminAddStep.mockReturnValue(new Promise((r) => { finish = r }))
    const sent = { x: 1, y: 1 }
    pin.current = sent
    mount()
    await screen.findByText('2. Step 2')
    fireEvent.click(screen.getByRole('button', { name: 'Add Step from Current View' }))
    await waitFor(() => expect(api.adminAddStep).toHaveBeenCalledTimes(1))
    // The user picks step 1 while the add is out.
    fireEvent.click(screen.getByText('1. Step 1'))
    await act(async () => finish({ id: 3, revision: 6 }))
    await waitFor(() => expect(pin.set).toHaveBeenCalled())
    // The pin is cleared only if it is still the one sent.
    const calls = pin.set.mock.calls
    const update = calls[calls.length - 1][0] as (p: unknown) => unknown
    expect(update(sent)).toBeNull()
    const placedMeanwhile = { x: 2, y: 2 }
    expect(update(placedMeanwhile)).toBe(placedMeanwhile)
    // And the step picked meanwhile stays selected.
    expect(screen.getByText('Selected: 1. Step 1')).toBeTruthy()
  })

  it('keeps a step picked while a step delete is out', async () => {
    // The delete clears the selection only if it is still the deleted step.
    api.adminGetTour.mockResolvedValue(detail(5, [step(1, 1), step(2, 2)]))
    let finish: (v: unknown) => void = () => {}
    api.adminDeleteStep.mockReturnValue(new Promise((r) => { finish = r }))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mount()
    await screen.findByText('2. Step 2')
    fireEvent.click(screen.getByText('2. Step 2'))
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[1])
    await waitFor(() => expect(api.adminDeleteStep).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByText('1. Step 1'))
    await act(async () => finish({ revision: 6 }))
    expect(screen.getByText('Selected: 1. Step 1')).toBeTruthy()
    vi.mocked(window.confirm).mockRestore()
  })

  it("doesn't save the title (Enter) while Delete tour is out", async () => {
    api.adminGetTour.mockResolvedValue(detail(5, [step(1, 1)]))
    api.adminDeleteTour.mockReturnValue(new Promise(() => {}))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mount()
    await screen.findByText('1. Step 1')
    fireEvent.click(screen.getByRole('button', { name: 'Edit title' }))
    const title = screen.getByDisplayValue('Tour') as HTMLInputElement
    fireEvent.change(title, { target: { value: 'Tour 2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Delete tour' }))
    await waitFor(() => expect(api.adminDeleteTour).toHaveBeenCalledTimes(1))
    fireEvent.keyDown(title, { key: 'Enter' })
    expect(api.adminUpdateTour).not.toHaveBeenCalled()
    vi.mocked(window.confirm).mockRestore()
  })
})
