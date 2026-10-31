// @vitest-environment jsdom
import React from 'react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The API module is the page's only I/O. The heavy sidebar sections and the
// row list are replaced by stubs so the test sees just the dashboard's own
// wiring (search filtering, delete flow); i18n and the toolbar are real.
const api = vi.hoisted(() => ({
  adminMe: vi.fn(),
  adminListSlides: vi.fn(),
  adminListAllTours: vi.fn(),
  adminListMetadataOptions: vi.fn(),
  adminGetViewerSettings: vi.fn(),
  staffGetViewerOverlayDefaults: vi.fn(),
  adminDeleteSlide: vi.fn(),
  adminBulkDeleteSlides: vi.fn(),
  adminBulkPublishSlides: vi.fn(),
  adminBulkSlideAttributes: vi.fn(),
  adminBulkTourVisibility: vi.fn(),
  adminBulkTourAttributes: vi.fn(),
  adminBulkDeleteTours: vi.fn(),
  adminStartExport: vi.fn(),
  adminCreateTour: vi.fn(),
  adminDuplicateTour: vi.fn(),
  clearAdminSession: vi.fn(),
  getRoleFromToken: vi.fn(),
  isAdminFromToken: vi.fn(),
  isManagerOrAboveFromToken: vi.fn(),
}))
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return { ...actual, ...api }
})

const listRenders = vi.hoisted(() => ({ lengths: [] as number[], confirmed: [] as boolean[] }))
// Memoised like the real SlideList: a re-render here means the dashboard handed
// it a new prop (an unstable handler), which is what would make typing lag.
vi.mock('../components/admin/SlideList', async () => {
  const R = await vi.importActual<typeof import('react')>('react')
  return {
    default: R.memo((p: {
      slides: { id: number; title: string; is_published?: boolean }[]
      slideTours: Record<number, { id: number; title: string; is_published: boolean; revision?: number | null; attributes?: Record<string, string[]> | null }[]>
      onDeleteSlide: (id: number, title: string) => void
      onCreateTour: (id: number) => void
      onDuplicateTour: (tour: { id: number; title: string }) => void
      onLinkClick: (e: React.MouseEvent<HTMLAnchorElement>, to: string) => void
      onToggleSelect: (id: number) => void
      onToggleSelectTour: (id: number) => void
      onOpenEdit: (slide: unknown) => void
      onConfirmUnpublish: (id: number) => Promise<boolean>
      onToursDrafted: (id: number) => void
    }) => {
      listRenders.lengths.push(p.slides.length)
      return (
        <div>
          {p.slides.map((s) => (
            <span key={s.id}>
              <button onClick={() => p.onDeleteSlide(s.id, s.title)}>delete {s.title}</button>
              <button onClick={() => p.onToggleSelect(s.id)}>select {s.title}</button>
              <button onClick={() => p.onCreateTour(s.id)}>tour {s.title}</button>
              <a href={`/admin/tours/${s.id}0`} onClick={(e) => p.onLinkClick(e, `/admin/tours/${s.id}0`)}>
                edit tour of {s.title}
              </a>
              <button onClick={() => p.onOpenEdit(s)}>edit {s.title}</button>
              <button onClick={() => { void p.onConfirmUnpublish(s.id).then((ok) => listRenders.confirmed.push(ok)) }}>
                row unpublish {s.title}
              </button>
              <button onClick={() => p.onToursDrafted(s.id)}>drafted {s.title}</button>
              <i data-testid={`pub-${s.id}`}>{String(s.is_published)}</i>
              {(p.slideTours[s.id] || []).map((t) => (
                <span key={t.id}>
                  <button onClick={() => p.onToggleSelectTour(t.id)}>select tour {t.id}</button>
                  <button onClick={() => p.onDuplicateTour(t)}>duplicate tour {t.id}</button>
                  <i data-testid={`tour-attrs-${t.id}`}>{JSON.stringify(t.attributes ?? {})}</i>
                </span>
              ))}
              <i data-testid={`tours-${s.id}`}>
                {(p.slideTours[s.id] || []).map((t) => `${t.id}:${t.is_published}:${t.revision}`).join(',')}
              </i>
            </span>
          ))}
        </div>
      )
    }),
  }
})
vi.mock('../components/admin/BulkCategoryModal', () => ({
  default: (p: { onApply: (key: string, values: string[], mode: 'add' | 'remove') => void }) => (
    <button onClick={() => p.onApply('organ', ['Liver'], 'add')}>apply category</button>
  ),
}))
vi.mock('../components/admin/SlideEditModal', () => ({
  default: (p: { onSaved: () => Promise<void> }) => (
    <button onClick={() => { p.onSaved().catch(() => {}) }}>save edit</button>
  ),
}))
// The sidebar sections expose just what the dashboard hands them: the Metadata
// tab's refresh signal, the Network tab's dirty report and guard exit, the
// import callback, sending report and guard exit.
const sections = vi.hoisted(() => ({
  networkDirty: false, networkLeave: vi.fn(), importSending: false, importLeave: vi.fn(),
}))
// Stands in for the upload section: a button that reports an upload running.
vi.mock('../components/admin/AdminUploadSection', () => ({
  default: (p: { onRunningChange: (running: boolean) => void }) => (
    <button onClick={() => p.onRunningChange(true)}>start upload</button>
  ),
}))
vi.mock('../components/admin/AdminMetadataSection', () => ({
  default: (p: { countsRefreshSignal: number }) => <i data-testid="meta-signal">{p.countsRefreshSignal}</i>,
}))
vi.mock('../components/admin/AdminViewerSettingsSection', () => ({
  default: (p: { form: { values: { siteTitle: string; maxIndexItems: number }; savedSnapshot: string } }) => (
    <i data-testid="vs-form">
      {`${p.form.values.siteTitle}|${p.form.values.maxIndexItems}|${p.form.savedSnapshot ? 'baseline' : 'none'}`}
    </i>
  ),
}))
vi.mock('../components/admin/AdminNetworkAuthSection', async () => {
  const R = await vi.importActual<typeof import('react')>('react')
  return {
    default: ({ onDirtyChange, leaveRef }: {
      onDirtyChange: (dirty: boolean) => void
      leaveRef?: { current: ((go: () => void) => void) | null }
    }) => {
      R.useEffect(() => { if (sections.networkDirty) onDirtyChange(true) }, [onDirtyChange])
      R.useEffect(() => {
        if (!leaveRef) return
        leaveRef.current = sections.networkLeave
        return () => { leaveRef.current = null }
      }, [leaveRef])
      return <div>NETWORK-SECTION</div>
    },
  }
})
vi.mock('../components/admin/AdminBackupSection', () => ({ default: () => <div>BACKUP-SECTION</div> }))
vi.mock('../components/admin/AdminImportSection', async () => {
  const R = await vi.importActual<typeof import('react')>('react')
  return {
    default: ({ onImported, onSendingChange, leaveRef }: {
      onImported?: () => void
      onSendingChange?: (sending: boolean) => void
      leaveRef?: { current: ((go: () => void) => void) | null }
    }) => {
      R.useEffect(() => { if (sections.importSending) onSendingChange?.(true) }, [onSendingChange])
      R.useEffect(() => {
        if (!leaveRef) return
        leaveRef.current = sections.importLeave
        return () => { leaveRef.current = null }
      }, [leaveRef])
      return <button onClick={() => onImported?.()}>imported</button>
    },
  }
})
vi.mock('../components/admin/AdminExportHistorySection', () => ({ default: () => null }))

import { I18nProvider } from '../lib/i18n'
import { RateLimitError } from '../lib/api'
import type { Slide } from '../lib/api'
import { STORAGE_KEYS } from '../lib/storageKeys'
import AdminDashboard from './AdminDashboard'

function slide(id: number, title: string): Slide {
  return {
    id, public_id: `p${id}`, catalog_id: '', title, thumb_url: '', width: 1, height: 1, mpp_x: 0, mpp_y: 0,
    vendor: '', wsi_filename: `${title}.svs`, attributes: {}, is_published: false,
    show_zoom_indicator: null, show_scale_bar: null, show_metadata_panel: null,
  } as Slide
}

const SLIDES = [slide(1, 'Liver'), slide(2, 'Kidney'), slide(3, 'Lung')]

function mount() {
  return render(
    <MemoryRouter>
      <I18nProvider>
        <AdminDashboard />
      </I18nProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  sections.networkDirty = false
  sections.importSending = false
  // Like the real guard's exit with no history entry to step off: go at once.
  sections.networkLeave.mockImplementation((go: () => void) => go())
  sections.importLeave.mockImplementation((go: () => void) => go())
  listRenders.lengths = []
  listRenders.confirmed = []
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} unobserve() {} })
  vi.stubGlobal('confirm', () => true)
  api.isAdminFromToken.mockReturnValue(false)
  api.isManagerOrAboveFromToken.mockReturnValue(true)
  api.getRoleFromToken.mockReturnValue('manager')
  api.adminMe.mockResolvedValue({ id: 1, username: 'm', role: 'manager', role_permissions: {} })
  api.adminListSlides.mockResolvedValue(SLIDES)
  api.adminListAllTours.mockResolvedValue([])
  api.adminListMetadataOptions.mockResolvedValue({ options: {} })
  api.staffGetViewerOverlayDefaults.mockResolvedValue({
    show_zoom_indicator: false, show_scale_bar: true, show_metadata_panel: true,
  })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.resetAllMocks()
})

describe('AdminDashboard slide search', () => {
  it('filters once after typing pauses, not on every keystroke', async () => {
    mount()
    await screen.findByText('delete Liver')
    // Let the page's own async loads (tours, metadata, ...) finish rendering
    // before counting, so only the keystrokes' effect is measured.
    await act(async () => { await new Promise((r) => setTimeout(r, 60)) })
    listRenders.lengths = []
    const input = screen.getByPlaceholderText(/search/i)
    fireEvent.change(input, { target: { value: 'k' } })
    fireEvent.change(input, { target: { value: 'ki' } })
    fireEvent.change(input, { target: { value: 'kid' } })
    // Still inside the debounce window: the list has not been re-filtered AND
    // was not re-rendered at all (its props - data and handlers - are stable).
    expect(screen.getByText('delete Liver')).toBeTruthy()
    expect(listRenders.lengths).toEqual([])
    await waitFor(() => expect(screen.queryByText('delete Liver')).toBeNull())
    expect(screen.getByText('delete Kidney')).toBeTruthy()
    // One narrowing for the whole burst (no intermediate 'k' / 'ki' lists).
    expect(listRenders.lengths.filter((n) => n < 3)).toEqual([1])
  })
})

describe('AdminDashboard slide list state across a page change', () => {
  it('comes back with the search and filters it was left with', async () => {
    api.adminListSlides.mockResolvedValue([
      { ...slide(1, 'Liver'), attributes: { stain: ['HE'] } },
      slide(2, 'Kidney'),
      { ...slide(3, 'Lung'), attributes: { stain: ['HE'] } },
    ])
    mount()
    await screen.findByText('delete Liver')
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: 'l' } })
    await waitFor(() => expect(screen.queryByText('delete Kidney')).toBeNull())
    // Leave the dashboard (it unmounts, like a visit to another admin page)…
    cleanup()
    // …and come back.
    mount()
    await screen.findByText('delete Liver')
    expect((screen.getByPlaceholderText(/search/i) as HTMLInputElement).value).toBe('l')
    expect(screen.queryByText('delete Kidney')).toBeNull()
  })

  it('shows a restored filter value no slide carries any more, so it can be turned off', async () => {
    sessionStorage.setItem(STORAGE_KEYS.adminSlideListState, JSON.stringify({
      attrFilters: { stain: ['Gone'] }, filtersOpen: true,
    }))
    mount()
    const chip = await screen.findByRole('button', { name: 'Gone' })
    expect(screen.queryByText('delete Liver')).toBeNull()
    fireEvent.click(chip)
    await screen.findByText('delete Liver')
    expect(screen.queryByRole('button', { name: 'Gone' })).toBeNull()
    expect(JSON.parse(sessionStorage.getItem(STORAGE_KEYS.adminSlideListState)!).attrFilters).toEqual({})
  })
})

describe('AdminDashboard slide delete', () => {
  it('does not report a failed delete when only the list refresh fails', async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    api.adminDeleteSlide.mockResolvedValue(undefined)
    mount()
    fireEvent.click(await screen.findByText('delete Liver'))
    // The refresh after the committed DELETE hits a throttle.
    api.adminListSlides.mockRejectedValue(new RateLimitError())
    await waitFor(() => expect(api.adminDeleteSlide).toHaveBeenCalledWith(1))
    await act(async () => { await Promise.resolve() })
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('reports a failed DELETE', async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    api.adminDeleteSlide.mockRejectedValue(new RateLimitError())
    mount()
    fireEvent.click(await screen.findByText('delete Liver'))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
  })
})


describe('AdminDashboard bulk actions', () => {
  it('after a committed bulk delete whose refresh fails: no "failed" message, deleted rows dropped, stale-list notice', async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    api.adminBulkDeleteSlides.mockResolvedValue({ ok: true, deleted: [1], not_found: [], failed: [] })
    mount()
    fireEvent.click(await screen.findByText('select Liver'))
    api.adminListSlides.mockRejectedValue(new RateLimitError())
    fireEvent.click(await screen.findByRole('button', { name: /^Delete$/ }))
    await waitFor(() => expect(api.adminBulkDeleteSlides).toHaveBeenCalled())
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    // The one message says the list is stale; it does not claim the delete failed.
    expect(alertSpy.mock.calls[0][0]).toMatch(/could not be refreshed/i)
    expect(alertSpy.mock.calls[0][0]).not.toMatch(/failed to delete/i)
    // The confirmed-deleted row is gone from the (otherwise stale) list.
    await waitFor(() => expect(screen.queryByText('delete Liver')).toBeNull())
    expect(screen.getByText('delete Kidney')).toBeTruthy()
  })

  it('after a committed bulk publish whose refresh fails: confirmed rows are patched locally and the admin is told the list is stale', async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    api.adminBulkPublishSlides.mockResolvedValue({
      ok: true,
      results: [{ slide_id: 1, ok: true, is_published: true, cascaded_tours: 0, error: null }],
    })
    mount()
    fireEvent.click(await screen.findByText('select Liver'))
    api.adminListSlides.mockRejectedValue(new RateLimitError())
    fireEvent.click(await screen.findByRole('button', { name: /^Publish$/ }))
    await waitFor(() => expect(api.adminBulkPublishSlides).toHaveBeenCalledWith([1], true))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toMatch(/could not be refreshed/i)
    expect(alertSpy.mock.calls[0][0]).not.toMatch(/failed to update publish state/i)
    expect(screen.getByTestId('pub-1').textContent).toBe('true')
    expect(screen.getByTestId('pub-2').textContent).toBe('false')
  })

  it('a bulk unpublish whose tours refresh fails (slides refresh fine) still tells the admin the view is stale', async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    api.adminBulkPublishSlides.mockResolvedValue({
      ok: true,
      results: [{ slide_id: 1, ok: true, is_published: false, cascaded_tours: 1, error: null }],
    })
    mount()
    fireEvent.click(await screen.findByText('select Liver'))
    // The initial tours load worked; the explicit refresh after the commit is throttled.
    await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
    api.adminListAllTours.mockRejectedValue(new RateLimitError())
    fireEvent.click(await screen.findByRole('button', { name: /^Unpublish$/ }))
    await waitFor(() => expect(api.adminBulkPublishSlides).toHaveBeenCalledWith([1], false))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toMatch(/could not be refreshed/i)
  })

  it('locks the slide list while a bulk action is out', async () => {
    // The action takes the selection at the click and clears it afterwards,
    // so a slide ticked meanwhile would be unticked unseen.
    let finish: (v: unknown) => void = () => {}
    api.adminBulkPublishSlides.mockReturnValue(new Promise((r) => { finish = r }))
    mount()
    const tick = await screen.findByText('select Liver')
    fireEvent.click(tick)
    fireEvent.click(await screen.findByRole('button', { name: /^Publish$/ }))
    await waitFor(() => expect(api.adminBulkPublishSlides).toHaveBeenCalled())
    expect(tick.closest('[inert]')).not.toBeNull()
    // The bar's select-all too: the answer clears the selection.
    expect((screen.getByRole('checkbox', { name: /^Select all/ }) as HTMLInputElement).disabled).toBe(true)
    await act(async () => finish({ ok: true, results: [{ slide_id: 1, ok: true, is_published: true, cascaded_tours: 0, error: null }] }))
    await waitFor(() => expect(screen.getByText('select Liver').closest('[inert]')).toBeNull())
  })

  it('locks the slide list while a row creates a tour or deletes its slide', async () => {
    // A second click would create a second tour, or delete twice.
    vi.stubGlobal('prompt', vi.fn(() => 'Tour A'))
    vi.stubGlobal('confirm', vi.fn(() => true))
    vi.stubGlobal('alert', vi.fn())
    let finishCreate: (v: unknown) => void = () => {}
    api.adminCreateTour.mockReturnValue(new Promise((r) => { finishCreate = r }))
    mount()
    const newTour = await screen.findByText('tour Liver')
    fireEvent.click(newTour)
    await waitFor(() => expect(api.adminCreateTour).toHaveBeenCalledTimes(1))
    expect(newTour.closest('[inert]')).not.toBeNull()
    await act(async () => finishCreate(Promise.reject(new Error('refused'))))
    await waitFor(() => expect(newTour.closest('[inert]')).toBeNull())

    let finishDelete: () => void = () => {}
    api.adminDeleteSlide.mockReturnValue(new Promise<void>((r) => { finishDelete = r }))
    const del = screen.getByText('delete Liver')
    fireEvent.click(del)
    await waitFor(() => expect(api.adminDeleteSlide).toHaveBeenCalledTimes(1))
    expect(del.closest('[inert]')).not.toBeNull()
    await act(async () => finishDelete())
  })

  it('holds the bulk bar while a row request is out', async () => {
    // Its refresh would race a bulk action's.
    vi.stubGlobal('prompt', vi.fn(() => 'Tour A'))
    api.adminCreateTour.mockReturnValue(new Promise(() => {}))
    mount()
    fireEvent.click(await screen.findByText('select Liver'))
    const publish = await screen.findByRole('button', { name: /^Publish$/ }) as HTMLButtonElement
    expect(publish.disabled).toBe(false)
    fireEvent.click(screen.getByText('tour Liver'))
    await waitFor(() => expect(publish.disabled).toBe(true))
  })

  it("doesn't leave for a new tour while an upload started meanwhile is running", async () => {
    // Leaving would cancel the upload; the tour is listed instead.
    vi.stubGlobal('prompt', vi.fn(() => 'Tour A'))
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    let finish: (v: unknown) => void = () => {}
    api.adminCreateTour.mockReturnValue(new Promise((r) => { finish = r }))
    mount()
    fireEvent.click(await screen.findByText('tour Liver'))
    await waitFor(() => expect(api.adminCreateTour).toHaveBeenCalled())
    const toursLoads = api.adminListAllTours.mock.calls.length
    fireEvent.click(screen.getByText('start upload'))
    await act(async () => finish({ id: 42 }))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledWith(expect.stringMatching(/would cancel the upload/)))
    expect(api.adminListAllTours.mock.calls.length).toBeGreaterThan(toursLoads)
  })

  it('a bulk publish whose refresh works stays quiet', async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    api.adminBulkPublishSlides.mockResolvedValue({
      ok: true,
      results: [{ slide_id: 1, ok: true, is_published: true, cascaded_tours: 0, error: null }],
    })
    mount()
    fireEvent.click(await screen.findByText('select Liver'))
    fireEvent.click(await screen.findByRole('button', { name: /^Publish$/ }))
    await waitFor(() => expect(api.adminBulkPublishSlides).toHaveBeenCalled())
    await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
    expect(alertSpy).not.toHaveBeenCalled()
  })

  it('after a committed bulk category change whose refresh fails: stale-list notice, not "failed to update categories"', async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    api.adminBulkSlideAttributes.mockResolvedValue({ ok: true, changed: [1], not_found: [] })
    mount()
    fireEvent.click(await screen.findByText('select Liver'))
    api.adminListSlides.mockRejectedValue(new RateLimitError())
    fireEvent.click(await screen.findByRole('button', { name: /Category|Categories/i }))
    fireEvent.click(await screen.findByText('apply category'))
    await waitFor(() => expect(api.adminBulkSlideAttributes).toHaveBeenCalledWith([1], 'organ', ['Liver'], 'add'))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toMatch(/could not be refreshed/i)
    expect(alertSpy.mock.calls[0][0]).not.toMatch(/failed to update categories/i)
  })
})

describe('AdminDashboard tour bulk actions', () => {
  const tour = (id: number, slideId: number, revision = 3) => ({
    id, public_id: `t${id}`, slide_public_id: `p${slideId}`, slide_id: slideId, title: `Tour ${id}`,
    description: '', is_published: false, unlisted: false, revision, attributes: {},
  })
  const tourBar = () => screen.getByRole('group', { name: /tour\(s\) selected/ })

  beforeEach(() => {
    api.adminListAllTours.mockResolvedValue([tour(10, 1), tour(20, 2, 5)])
  })

  it('selects tours separately from slides and shows only the tour bar for them', async () => {
    mount()
    fireEvent.click(await screen.findByText('select tour 10'))
    expect(within(tourBar()).getByText('1 tour(s) selected')).toBeTruthy()
    // No slide is selected, so the slide bar (with its Publish action) is not shown.
    expect(screen.queryByRole('button', { name: /^Publish$/ })).toBeNull()
    fireEvent.click(screen.getByText('select Liver'))
    expect(screen.getByRole('button', { name: /^Publish$/ })).toBeTruthy()
    expect(within(tourBar()).getByText('1 tour(s) selected')).toBeTruthy()
  })

  it('moves the selected tours, with their listed revisions, and reports the skipped ones', async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    api.adminBulkTourVisibility.mockResolvedValue({
      ok: true,
      results: [
        { tour_id: 10, ok: false, changed: false, error: 'parent_draft' },
        { tour_id: 20, ok: true, changed: true, error: null, is_published: true, unlisted: false, revision: 6 },
      ],
    })
    mount()
    fireEvent.click(await screen.findByText('select tour 10'))
    fireEvent.click(screen.getByText('select tour 20'))
    const toursLoads = api.adminListAllTours.mock.calls.length
    fireEvent.click(within(tourBar()).getByRole('button', { name: 'Open' }))
    await waitFor(() => expect(api.adminBulkTourVisibility).toHaveBeenCalledTimes(1))
    const [sent, target] = api.adminBulkTourVisibility.mock.calls[0]
    expect(target).toBe('public')
    expect(sent.map((x: { id: number; revision: number }) => [x.id, x.revision])).toEqual([[10, 3], [20, 5]])
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toMatch(/1 tour\(s\) skipped: their slide is a draft/)
    // The tours were reloaded, and the skipped one stays selected for a retry.
    expect(api.adminListAllTours.mock.calls.length).toBeGreaterThan(toursLoads)
    expect(within(tourBar()).getByText('1 tour(s) selected')).toBeTruthy()
  })

  it('leaves a selected tour whose slide is filtered out alone, and selected', async () => {
    api.adminBulkTourVisibility.mockResolvedValue({
      ok: true, results: [{ tour_id: 20, ok: true, changed: true, error: null, is_published: false, unlisted: false, revision: 6 }],
    })
    mount()
    fireEvent.click(await screen.findByText('select tour 10'))
    fireEvent.click(screen.getByText('select tour 20'))
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: 'kid' } })
    await waitFor(() => expect(screen.queryByText('delete Liver')).toBeNull())
    expect(within(tourBar()).getByText('1 tour(s) selected')).toBeTruthy()
    fireEvent.click(within(tourBar()).getByRole('button', { name: 'Draft' }))
    await waitFor(() => expect(api.adminBulkTourVisibility).toHaveBeenCalledTimes(1))
    expect(api.adminBulkTourVisibility.mock.calls[0][0].map((x: { id: number }) => x.id)).toEqual([20])
    // The acted-on tour leaves the selection; the hidden one is still selected
    // once its slide is shown again.
    await waitFor(() => expect(screen.queryByRole('group', { name: /tour\(s\) selected/ })).toBeNull())
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: '' } })
    await screen.findByText('delete Liver')
    expect(within(tourBar()).getByText('1 tour(s) selected')).toBeTruthy()
  })

  it("changes the selected tours' own metadata from the tour bar", async () => {
    api.adminBulkTourAttributes.mockResolvedValue({
      ok: true, results: [{ tour_id: 10, ok: true, changed: true, error: null, revision: 4 }],
    })
    mount()
    fireEvent.click(await screen.findByText('select tour 10'))
    fireEvent.click(within(tourBar()).getByRole('button', { name: 'Metadata' }))
    fireEvent.click(await screen.findByText('apply category'))
    await waitFor(() => expect(api.adminBulkTourAttributes).toHaveBeenCalledTimes(1))
    const [sent, key, values, mode] = api.adminBulkTourAttributes.mock.calls[0]
    expect([sent.map((x: { id: number }) => x.id), key, values, mode]).toEqual([[10], 'organ', ['Liver'], 'add'])
    await waitFor(() => expect(screen.queryByText('apply category')).toBeNull())
  })

  it('confirms a bulk tour delete, and drops the deleted tours when the refresh fails', async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    const confirmSpy = vi.fn((_message?: string) => true)
    vi.stubGlobal('confirm', confirmSpy)
    api.adminBulkDeleteTours.mockResolvedValue({ ok: true, deleted: [10], not_found: [], failed: [] })
    mount()
    fireEvent.click(await screen.findByText('select tour 10'))
    api.adminListAllTours.mockRejectedValue(new RateLimitError())
    fireEvent.click(within(tourBar()).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(api.adminBulkDeleteTours).toHaveBeenCalledWith([10]))
    expect(confirmSpy.mock.calls[0][0]).toMatch(/Delete 1 selected tour/)
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toMatch(/could not be refreshed/i)
    await waitFor(() => expect(screen.queryByText('select tour 10')).toBeNull())
    expect(screen.getByText('select tour 20')).toBeTruthy()
    expect(screen.queryByRole('group', { name: /tour\(s\) selected/ })).toBeNull()
  })

  it('applies a metadata change to the listed tours when the reload fails', async () => {
    vi.stubGlobal('alert', vi.fn())
    api.adminListAllTours.mockResolvedValue([{ ...tour(10, 1), attributes: { organ: ['Kidney'], stain: ['PAS'] } }, tour(20, 2, 5)])
    api.adminBulkTourAttributes.mockResolvedValue({
      ok: true, results: [{ tour_id: 10, ok: true, changed: true, error: null, revision: 4 }],
    })
    mount()
    fireEvent.click(await screen.findByText('select tour 10'))
    await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
    api.adminListAllTours.mockRejectedValue(new RateLimitError())
    fireEvent.click(within(tourBar()).getByRole('button', { name: 'Metadata' }))
    fireEvent.click(await screen.findByText('apply category'))
    await waitFor(() => expect(JSON.parse(screen.getByTestId('tour-attrs-10').textContent!)).toEqual({
      organ: ['Kidney', 'Liver'], stain: ['PAS'],
    }))
    expect(screen.getByTestId('tours-1').textContent).toBe('10:false:4')
  })

  it('keeps a tour unticked while the request was in flight unticked, even when it failed', async () => {
    vi.stubGlobal('alert', vi.fn())
    let resolve!: (v: unknown) => void
    api.adminBulkTourVisibility.mockReturnValue(new Promise((r) => { resolve = r }))
    mount()
    fireEvent.click(await screen.findByText('select tour 10'))
    fireEvent.click(screen.getByText('select tour 20'))
    fireEvent.click(within(tourBar()).getByRole('button', { name: 'Draft' }))
    await waitFor(() => expect(api.adminBulkTourVisibility).toHaveBeenCalled())
    fireEvent.click(screen.getByText('select tour 20'))  // untick mid-flight
    await act(async () => {
      resolve({ ok: true, results: [
        { tour_id: 10, ok: false, changed: false, error: 'conflict' },
        { tour_id: 20, ok: false, changed: false, error: 'conflict' },
      ] })
      await new Promise((r) => setTimeout(r, 30))
    })
    // Tour 10 stays selected for the retry; tour 20 is not brought back.
    expect(within(tourBar()).getByText('1 tour(s) selected')).toBeTruthy()
    fireEvent.click(within(tourBar()).getByRole('button', { name: 'Draft' }))
    await waitFor(() => expect(api.adminBulkTourVisibility).toHaveBeenCalledTimes(2))
    expect(api.adminBulkTourVisibility.mock.calls[1][0].map((x: { id: number }) => x.id)).toEqual([10])
  })

  it('does not keep tours selected that a retry cannot fix', async () => {
    vi.stubGlobal('alert', vi.fn())
    api.adminBulkTourAttributes.mockResolvedValue({ ok: true, results: [
      { tour_id: 10, ok: false, changed: false, error: 'not_registered' },
      { tour_id: 20, ok: false, changed: false, error: 'not_found' },
    ] })
    mount()
    fireEvent.click(await screen.findByText('select tour 10'))
    fireEvent.click(screen.getByText('select tour 20'))
    fireEvent.click(within(tourBar()).getByRole('button', { name: 'Metadata' }))
    fireEvent.click(await screen.findByText('apply category'))
    await waitFor(() => expect(screen.queryByRole('group', { name: /tour\(s\) selected/ })).toBeNull())
  })

  it("reports a failed delete without the server's error text, keeping the tour selected", async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    api.adminBulkDeleteTours.mockResolvedValue({ ok: true, deleted: [], not_found: [], failed: [{ tour_id: 10, error: 'failed' }] })
    mount()
    fireEvent.click(await screen.findByText('select tour 10'))
    fireEvent.click(within(tourBar()).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toBe('Failed to delete 1 tour(s)')
    expect(within(tourBar()).getByText('1 tour(s) selected')).toBeTruthy()
  })

  it('reloads the tours after a request that failed as a whole, as some tours may have committed', async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    api.adminBulkTourVisibility.mockRejectedValue(new Error('504 Gateway Timeout'))
    mount()
    fireEvent.click(await screen.findByText('select tour 10'))
    await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
    // The server did publish tour 10 (revision 4) before the response was lost.
    api.adminListAllTours.mockResolvedValue([{ ...tour(10, 1), is_published: true, revision: 4 }, tour(20, 2, 5)])
    fireEvent.click(within(tourBar()).getByRole('button', { name: 'Open' }))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toMatch(/Failed to update tour visibility/)
    await waitFor(() => expect(screen.getByTestId('tours-1').textContent).toBe('10:true:4'))
  })

  it("does not show the slides' export error above the tour bar", async () => {
    api.isAdminFromToken.mockReturnValue(true)
    api.adminStartExport.mockRejectedValue(new Error('export refused'))
    mount()
    fireEvent.click(await screen.findByText('select Liver'))
    fireEvent.click(await screen.findByRole('button', { name: 'Export selected' }))
    // Shown with the slide bar, where it belongs.
    expect((await screen.findByRole('alert')).textContent).toMatch(/export refused/)
    fireEvent.click(screen.getByText('select Liver'))
    fireEvent.click(screen.getByText('select tour 10'))
    expect(tourBar()).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('does nothing when the delete is not confirmed', async () => {
    vi.stubGlobal('confirm', () => false)
    mount()
    fireEvent.click(await screen.findByText('select tour 10'))
    fireEvent.click(within(tourBar()).getByRole('button', { name: 'Delete' }))
    expect(api.adminBulkDeleteTours).not.toHaveBeenCalled()
  })
})

describe('AdminDashboard handlers handed to the memoised list', () => {
  it('create-tour reads the tours loaded after the list first rendered (no stale closure)', async () => {
    const promptSpy = vi.fn().mockReturnValue(null)
    vi.stubGlobal('prompt', promptSpy)
    api.adminListAllTours.mockResolvedValue([
      { id: 10, slide_id: 1, title: 'T', is_published: false, public_id: 'tp10' },
    ])
    mount()
    const btn = await screen.findByText('tour Liver')
    // The tours arrive after the first paint; SlideList is memoised and gets the
    // same onCreateTour throughout, so it must see them through the stable
    // wrapper. Clicking is harmless to repeat (the prompt is cancelled).
    await waitFor(() => {
      fireEvent.click(btn)
      expect(promptSpy).toHaveBeenLastCalledWith(expect.any(String), 'Slide 1 Tour 2')
    })
  })
})

const TOURS_FAILED = /tours list could not be loaded/i
const tabButton = (name: string) =>
  screen.queryAllByRole('button').find((b) => b.textContent === name) ?? null

function asAdmin() {
  api.isAdminFromToken.mockReturnValue(true)
  api.getRoleFromToken.mockReturnValue('admin')
  api.adminMe.mockResolvedValue({ id: 1, username: 'a', role: 'admin', role_permissions: {} })
  api.adminGetViewerSettings.mockResolvedValue({})
}

describe('AdminDashboard tours list load failure', () => {
  it('shows a notice with a retry instead of silently dropping the tours', async () => {
    api.adminListAllTours.mockRejectedValueOnce(new RateLimitError()).mockResolvedValue([])
    mount()
    expect(await screen.findByText(TOURS_FAILED)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByText(TOURS_FAILED)).toBeNull())
    expect(api.adminListAllTours).toHaveBeenCalledTimes(2)
  })

  it('an unpublish whose tours never loaded fetches them first and warns about the cascade', async () => {
    const confirmSpy = vi.fn((_message?: string) => false)
    vi.stubGlobal('confirm', confirmSpy)
    api.adminListAllTours
      .mockRejectedValueOnce(new RateLimitError())
      .mockResolvedValue([{ id: 10, slide_id: 1, title: 'T', is_published: true, public_id: 'tp10' }])
    mount()
    fireEvent.click(await screen.findByText('select Liver'))
    await screen.findByText(TOURS_FAILED)
    fireEvent.click(await screen.findByRole('button', { name: /^Unpublish$/ }))
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    expect(confirmSpy.mock.calls[0][0]).toMatch(/1 published tour/)
    // Declined: nothing was unpublished.
    expect(api.adminBulkPublishSlides).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByText(TOURS_FAILED)).toBeNull())
  })

  it('asks without a count when the tours still cannot be loaded', async () => {
    const confirmSpy = vi.fn((_message?: string) => true)
    vi.stubGlobal('confirm', confirmSpy)
    vi.stubGlobal('alert', vi.fn())
    api.adminListAllTours.mockRejectedValue(new RateLimitError())
    api.adminBulkPublishSlides.mockResolvedValue({
      ok: true, results: [{ slide_id: 1, ok: true, is_published: false, cascaded_tours: 1, error: null }],
    })
    mount()
    fireEvent.click(await screen.findByText('select Liver'))
    await screen.findByText(TOURS_FAILED)
    fireEvent.click(await screen.findByRole('button', { name: /^Unpublish$/ }))
    await waitFor(() => expect(api.adminBulkPublishSlides).toHaveBeenCalledWith([1], false))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(confirmSpy.mock.calls[0][0]).toMatch(/could not be checked/)
  })
})

describe('AdminDashboard per-row unpublish', () => {
  it('warns with the count of published tours on the slide', async () => {
    const confirmSpy = vi.fn((_message?: string) => false)
    vi.stubGlobal('confirm', confirmSpy)
    api.adminListAllTours.mockResolvedValue([
      { id: 10, slide_id: 1, title: 'T', is_published: true, public_id: 'tp10' },
      { id: 11, slide_id: 2, title: 'U', is_published: true, public_id: 'tp11' },
    ])
    mount()
    await waitFor(() => expect(screen.getByTestId('tours-1').textContent).not.toBe(''))
    fireEvent.click(screen.getByText('row unpublish Liver'))
    await waitFor(() => expect(listRenders.confirmed).toEqual([false]))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(confirmSpy.mock.calls[0][0]).toMatch(/1 published tour /)
  })

  it('goes ahead without asking when the slide has no published tour', async () => {
    const confirmSpy = vi.fn((_message?: string) => false)
    vi.stubGlobal('confirm', confirmSpy)
    mount()
    await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
    fireEvent.click(screen.getByText('row unpublish Liver'))
    await waitFor(() => expect(listRenders.confirmed).toEqual([true]))
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('fetches the tours first when their list failed to load, like the bulk path', async () => {
    const confirmSpy = vi.fn((_message?: string) => true)
    vi.stubGlobal('confirm', confirmSpy)
    api.adminListAllTours
      .mockRejectedValueOnce(new RateLimitError())
      .mockResolvedValue([{ id: 10, slide_id: 1, title: 'T', is_published: true, public_id: 'tp10' }])
    mount()
    await screen.findByText(TOURS_FAILED)
    fireEvent.click(screen.getByText('row unpublish Liver'))
    await waitFor(() => expect(listRenders.confirmed).toEqual([true]))
    expect(api.adminListAllTours).toHaveBeenCalledTimes(2)
    expect(confirmSpy.mock.calls[0][0]).toMatch(/1 published tour /)
    await waitFor(() => expect(screen.queryByText(TOURS_FAILED)).toBeNull())
  })

  it('asks without a count when the tours still cannot be loaded', async () => {
    const confirmSpy = vi.fn((_message?: string) => false)
    vi.stubGlobal('confirm', confirmSpy)
    api.adminListAllTours.mockRejectedValue(new RateLimitError())
    mount()
    await screen.findByText(TOURS_FAILED)
    fireEvent.click(screen.getByText('row unpublish Liver'))
    await waitFor(() => expect(listRenders.confirmed).toEqual([false]))
    expect(confirmSpy.mock.calls[0][0]).toMatch(/could not be checked/)
  })

  it("mirrors the cascade on the slide's current tours, bumping each revision", async () => {
    api.adminListAllTours.mockResolvedValue([
      { id: 10, slide_id: 1, title: 'T', is_published: true, public_id: 'tp10', revision: 3 },
      { id: 11, slide_id: 1, title: 'U', is_published: false, public_id: 'tp11', revision: 1 },
      { id: 12, slide_id: 2, title: 'V', is_published: true, public_id: 'tp12', revision: 5 },
    ])
    mount()
    await waitFor(() => expect(screen.getByTestId('tours-1').textContent).toBe('10:true:3,11:false:1'))
    fireEvent.click(screen.getByText('drafted Liver'))
    await waitFor(() => expect(screen.getByTestId('tours-1').textContent).toBe('10:false:4,11:false:1'))
    expect(screen.getByTestId('tours-2').textContent).toBe('12:true:5')
  })
})

describe('AdminDashboard unpublish before the tours list has answered', () => {
  // The first tours request never answers; the check's own request does.
  const firstLoadPending = () => api.adminListAllTours.mockReturnValueOnce(new Promise(() => {}))

  it('bulk: fetches the tours instead of reading the empty placeholder as "no tours"', async () => {
    const confirmSpy = vi.fn((_message?: string) => false)
    vi.stubGlobal('confirm', confirmSpy)
    firstLoadPending().mockResolvedValue([{ id: 10, slide_id: 1, title: 'T', is_published: true, public_id: 'tp10' }])
    mount()
    fireEvent.click(await screen.findByText('select Liver'))
    fireEvent.click(await screen.findByRole('button', { name: /^Unpublish$/ }))
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(1))
    expect(confirmSpy.mock.calls[0][0]).toMatch(/1 published tour/)
    expect(api.adminListAllTours).toHaveBeenCalledTimes(2)
    expect(api.adminBulkPublishSlides).not.toHaveBeenCalled()
  })

  it('per row: fetches the tours first and warns with the count', async () => {
    const confirmSpy = vi.fn((_message?: string) => false)
    vi.stubGlobal('confirm', confirmSpy)
    firstLoadPending().mockResolvedValue([{ id: 10, slide_id: 1, title: 'T', is_published: true, public_id: 'tp10' }])
    mount()
    fireEvent.click(await screen.findByText('row unpublish Liver'))
    await waitFor(() => expect(listRenders.confirmed).toEqual([false]))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(confirmSpy.mock.calls[0][0]).toMatch(/1 published tour /)
  })

  it('per row: asks without a count when that fetch fails too', async () => {
    const confirmSpy = vi.fn((_message?: string) => false)
    vi.stubGlobal('confirm', confirmSpy)
    firstLoadPending().mockRejectedValue(new RateLimitError())
    mount()
    fireEvent.click(await screen.findByText('row unpublish Liver'))
    await waitFor(() => expect(listRenders.confirmed).toEqual([false]))
    expect(confirmSpy.mock.calls[0][0]).toMatch(/could not be checked/)
  })
})

describe('AdminDashboard sidebar tabs per role', () => {
  it('shows a manager only their own tabs while /me is still loading', async () => {
    api.adminMe.mockReturnValue(new Promise(() => {}))
    mount()
    await screen.findByText('delete Liver')
    expect(tabButton('Upload')).toBeTruthy()
    expect(tabButton('Metadata')).toBeTruthy()
    for (const name of ['Settings', 'Network', 'Backup & Restore', 'Import / Export']) {
      expect(tabButton(name)).toBeNull()
    }
  })

  it('keeps an instructor on the instructor tabs when /me fails, and retries /me', async () => {
    api.isManagerOrAboveFromToken.mockReturnValue(false)
    api.getRoleFromToken.mockReturnValue('instructor')
    api.adminMe
      .mockRejectedValueOnce(new RateLimitError())
      .mockResolvedValue({ id: 3, username: 'inst', role: 'instructor', role_permissions: {} })
    mount()
    await screen.findByText('delete Liver')
    await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
    expect(tabButton('Slides')).toBeTruthy()
    for (const name of ['Upload', 'Metadata', 'Network', 'Backup & Restore']) {
      expect(tabButton(name)).toBeNull()
    }
    await waitFor(() => expect(api.adminMe).toHaveBeenCalledTimes(2), { timeout: 3000 })
    expect(await screen.findByText(/inst/)).toBeTruthy()
  })

  it('does not drop a post-restore reopen while the role is still unknown', async () => {
    sessionStorage.setItem(STORAGE_KEYS.adminPostRestore, 'null')
    api.getRoleFromToken.mockReturnValue(null)
    api.adminMe.mockReturnValue(new Promise(() => {}))
    mount()
    await screen.findByText('delete Liver')
    expect(screen.queryByText('BACKUP-SECTION')).toBeNull()
    expect(sessionStorage.getItem(STORAGE_KEYS.adminPostRestore)).toBe('null')
  })
})

describe('AdminDashboard Logout', () => {
  it('asks first, and keeps the session when declined', async () => {
    const confirmSpy = vi.fn((_msg?: string) => false)
    vi.stubGlobal('confirm', confirmSpy)
    render(
      <MemoryRouter initialEntries={['/admin/dashboard']}>
        <I18nProvider>
          <Routes>
            <Route path="/admin/dashboard" element={<AdminDashboard />} />
            <Route path="/admin" element={<div>LOGIN-PAGE</div>} />
          </Routes>
        </I18nProvider>
      </MemoryRouter>,
    )
    await screen.findByText('delete Liver')
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(confirmSpy.mock.calls).toEqual([['Log out?']])
    expect(api.clearAdminSession).not.toHaveBeenCalled()
    expect(screen.getByText('delete Liver')).toBeTruthy()
    confirmSpy.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(confirmSpy).toHaveBeenCalledTimes(2)
    expect(api.clearAdminSession).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
  })
})

describe('AdminDashboard unsaved Network edits', () => {
  function mountRoutes() {
    return render(
      <MemoryRouter initialEntries={['/admin/dashboard']}>
        <I18nProvider>
          <Routes>
            <Route path="/admin/dashboard" element={<AdminDashboard />} />
            <Route path="/admin/account" element={<div>ACCOUNT-PAGE</div>} />
            <Route path="/admin/tours/:id" element={<div>TOUR-EDITOR</div>} />
            <Route path="/admin" element={<div>LOGIN-PAGE</div>} />
          </Routes>
        </I18nProvider>
      </MemoryRouter>,
    )
  }

  async function openDirtyNetworkTab() {
    asAdmin()
    sections.networkDirty = true
    mountRoutes()
    await screen.findByText('delete Liver')
    fireEvent.click(await waitFor(() => tabButton('Network')!))
    expect(screen.getByText('NETWORK-SECTION')).toBeTruthy()
  }

  // Every confirmed exit goes through the section's guard exit (which steps
  // off the guard's history entry before navigating), not straight to `nav`.
  it('asks before a header link drops them', async () => {
    const confirmSpy = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmSpy)
    await openDirtyNetworkTab()
    fireEvent.click(screen.getByText('Account'))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(screen.getByText('NETWORK-SECTION')).toBeTruthy()
    expect(sections.networkLeave).not.toHaveBeenCalled()
    confirmSpy.mockReturnValue(true)
    fireEvent.click(screen.getByText('Account'))
    expect(await screen.findByText('ACCOUNT-PAGE')).toBeTruthy()
    expect(sections.networkLeave).toHaveBeenCalledTimes(1)
  })

  it('navigates only once the guard exit runs', async () => {
    vi.stubGlobal('confirm', () => true)
    let go: (() => void) | null = null
    sections.networkLeave.mockImplementation((g: () => void) => { go = g })
    await openDirtyNetworkTab()
    fireEvent.click(screen.getByText('Account'))
    // Still here while the guard steps back off its entry...
    expect(screen.getByText('NETWORK-SECTION')).toBeTruthy()
    act(() => go!())
    // ...and on to the link's page from the dashboard's own entry.
    expect(await screen.findByText('ACCOUNT-PAGE')).toBeTruthy()
  })

  it('leaves a modified header-link click to the browser', async () => {
    const confirmSpy = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmSpy)
    await openDirtyNetworkTab()
    expect(fireEvent.click(screen.getByText('Account'), { ctrlKey: true })).toBe(true)
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(sections.networkLeave).not.toHaveBeenCalled()
    expect(screen.getByText('NETWORK-SECTION')).toBeTruthy()
  })

  it('asks before Logout drops them', async () => {
    // Yes to logging out, no to dropping the edits.
    const confirmSpy = vi.fn((msg?: string) => msg === 'Log out?')
    vi.stubGlobal('confirm', confirmSpy)
    await openDirtyNetworkTab()
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(confirmSpy.mock.calls).toEqual([
      ['Log out?'],
      ['You have unsaved changes that will be lost. Leave anyway?'],
    ])
    expect(api.clearAdminSession).not.toHaveBeenCalled()
    expect(sections.networkLeave).not.toHaveBeenCalled()
    confirmSpy.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(api.clearAdminSession).toHaveBeenCalledTimes(1)
    expect(sections.networkLeave).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
  })

  it('asks before a sidebar tab switch drops them, then leaves through the guard', async () => {
    const confirmSpy = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmSpy)
    await openDirtyNetworkTab()
    fireEvent.click(tabButton('Settings')!)
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(screen.getByText('NETWORK-SECTION')).toBeTruthy()
    confirmSpy.mockReturnValue(true)
    fireEvent.click(tabButton('Settings')!)
    expect(sections.networkLeave).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByText('NETWORK-SECTION')).toBeNull())
  })

  it('leaves without asking when nothing is unsaved, still through the guard', async () => {
    // e.g. after saving: the guard's history entry is still in place.
    const confirmSpy = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmSpy)
    asAdmin()
    mountRoutes()
    fireEvent.click(await waitFor(() => tabButton('Network')!))
    fireEvent.click(screen.getByText('Account'))
    expect(await screen.findByText('ACCOUNT-PAGE')).toBeTruthy()
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(sections.networkLeave).toHaveBeenCalledTimes(1)
  })

  it('navigates directly from the other tabs', async () => {
    asAdmin()
    mountRoutes()
    fireEvent.click(await screen.findByText('Account'))
    expect(await screen.findByText('ACCOUNT-PAGE')).toBeTruthy()
    expect(sections.networkLeave).not.toHaveBeenCalled()
  })

  it("asks before a slide list link to a tour's editor drops them, then leaves through the guard", async () => {
    const confirmSpy = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmSpy)
    await openDirtyNetworkTab()
    const link = screen.getByText('edit tour of Liver')
    fireEvent.click(link)
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(sections.networkLeave).not.toHaveBeenCalled()
    expect(screen.getByText('NETWORK-SECTION')).toBeTruthy()
    // A modified click is the browser's (a new tab): nothing is asked.
    expect(fireEvent.click(link, { ctrlKey: true })).toBe(true)
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    confirmSpy.mockReturnValue(true)
    fireEvent.click(link)
    expect(await screen.findByText('TOUR-EDITOR')).toBeTruthy()
    expect(sections.networkLeave).toHaveBeenCalledTimes(1)
  })

  it('asks before New Tour drops them - ahead of the title prompt - then leaves through the guard', async () => {
    const confirmSpy = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmSpy)
    const promptSpy = vi.fn(() => 'My tour')
    vi.stubGlobal('prompt', promptSpy)
    api.adminCreateTour.mockResolvedValue({ id: 42 })
    await openDirtyNetworkTab()
    fireEvent.click(screen.getByText('tour Liver'))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(promptSpy).not.toHaveBeenCalled()
    expect(api.adminCreateTour).not.toHaveBeenCalled()

    confirmSpy.mockReturnValue(true)
    fireEvent.click(screen.getByText('tour Liver'))
    expect(await screen.findByText('TOUR-EDITOR')).toBeTruthy()
    expect(promptSpy).toHaveBeenCalledTimes(1)
    expect(api.adminCreateTour).toHaveBeenCalledWith(1, 'My tour')
    expect(sections.networkLeave).toHaveBeenCalledTimes(1)
  })

  it('duplicates a tour under the title asked for, then opens the copy in its editor', async () => {
    const promptSpy = vi.fn(() => 'Liver tour v2')
    vi.stubGlobal('prompt', promptSpy)
    api.adminListAllTours.mockResolvedValue([
      { id: 10, slide_id: 1, title: 'Liver tour', is_published: true, public_id: 'tp10' },
    ])
    api.adminDuplicateTour.mockResolvedValue({ id: 77, public_id: 'tp77' })
    asAdmin()
    mountRoutes()
    fireEvent.click(await screen.findByText('duplicate tour 10'))
    expect(promptSpy).toHaveBeenCalledWith(expect.any(String), 'Liver tour (copy)')
    expect(await screen.findByText('TOUR-EDITOR')).toBeTruthy()
    expect(api.adminDuplicateTour).toHaveBeenCalledWith(10, 'Liver tour v2')
  })

  it('stays on the dashboard when the duplicate is cancelled or fails', async () => {
    const alertSpy = vi.fn()
    vi.stubGlobal('alert', alertSpy)
    const promptSpy = vi.fn<() => string | null>(() => null)
    vi.stubGlobal('prompt', promptSpy)
    api.adminListAllTours.mockResolvedValue([
      { id: 10, slide_id: 1, title: 'Liver tour', is_published: false, public_id: 'tp10' },
    ])
    api.adminDuplicateTour.mockRejectedValue(new Error('no room'))
    asAdmin()
    mountRoutes()
    fireEvent.click(await screen.findByText('duplicate tour 10'))
    expect(api.adminDuplicateTour).not.toHaveBeenCalled()

    promptSpy.mockReturnValue('Copy')
    fireEvent.click(screen.getByText('duplicate tour 10'))
    await waitFor(() => expect(alertSpy).toHaveBeenCalledTimes(1))
    expect(alertSpy.mock.calls[0][0]).toMatch(/Failed to duplicate tour/)
    expect(screen.queryByText('TOUR-EDITOR')).toBeNull()
  })

  it('keeps guarding the edits when the New Tour prompt is cancelled after the confirm', async () => {
    const confirmSpy = vi.fn(() => true)
    vi.stubGlobal('confirm', confirmSpy)
    vi.stubGlobal('prompt', () => null)
    await openDirtyNetworkTab()
    fireEvent.click(screen.getByText('tour Liver'))
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(screen.getByText('NETWORK-SECTION')).toBeTruthy()
    // The edits are still there, so leaving still asks.
    confirmSpy.mockReturnValue(false)
    fireEvent.click(tabButton('Settings')!)
    expect(confirmSpy).toHaveBeenCalledTimes(2)
    expect(screen.getByText('NETWORK-SECTION')).toBeTruthy()
  })
})

describe('AdminDashboard bundle upload on the Import / Export tab', () => {
  async function openSendingImportTab() {
    asAdmin()
    sections.importSending = true
    render(
      <MemoryRouter initialEntries={['/admin/dashboard']}>
        <I18nProvider>
          <Routes>
            <Route path="/admin/dashboard" element={<AdminDashboard />} />
            <Route path="/admin/account" element={<div>ACCOUNT-PAGE</div>} />
            <Route path="/admin" element={<div>LOGIN-PAGE</div>} />
          </Routes>
        </I18nProvider>
      </MemoryRouter>,
    )
    await screen.findByText('delete Liver')
    fireEvent.click(await waitFor(() => tabButton('Import / Export')!))
    expect(screen.getByText('imported')).toBeTruthy()
  }

  it('asks before a tab switch cancels it, then leaves through the guard', async () => {
    const confirmSpy = vi.fn((_msg?: string) => false)
    vi.stubGlobal('confirm', confirmSpy)
    await openSendingImportTab()
    fireEvent.click(tabButton('Settings')!)
    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(confirmSpy.mock.calls[0][0]).toMatch(/still being uploaded/)
    expect(screen.getByText('imported')).toBeTruthy()
    expect(sections.importLeave).not.toHaveBeenCalled()
    confirmSpy.mockReturnValue(true)
    fireEvent.click(tabButton('Settings')!)
    expect(sections.importLeave).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByText('imported')).toBeNull())
  })

  it('asks before a header link or Logout cancels it', async () => {
    // Yes to logging out, no to cancelling the upload.
    const confirmSpy = vi.fn((msg?: string) => msg === 'Log out?')
    vi.stubGlobal('confirm', confirmSpy)
    await openSendingImportTab()
    fireEvent.click(screen.getByText('Account'))
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(confirmSpy).toHaveBeenCalledTimes(3)
    expect(confirmSpy.mock.calls[1][0]).toBe('Log out?')
    expect(confirmSpy.mock.calls[2][0]).toMatch(/still being uploaded/)
    expect(api.clearAdminSession).not.toHaveBeenCalled()
    expect(screen.getByText('imported')).toBeTruthy()
    confirmSpy.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(await screen.findByText('LOGIN-PAGE')).toBeTruthy()
    expect(sections.importLeave).toHaveBeenCalledTimes(1)
  })

  it('leaves without asking once nothing is being sent', async () => {
    const confirmSpy = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmSpy)
    asAdmin()
    render(
      <MemoryRouter>
        <I18nProvider>
          <AdminDashboard />
        </I18nProvider>
      </MemoryRouter>,
    )
    fireEvent.click(await waitFor(() => tabButton('Import / Export')!))
    fireEvent.click(tabButton('Settings')!)
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(sections.importLeave).toHaveBeenCalledTimes(1)
  })
})

describe('AdminDashboard viewer settings', () => {
  it('loads the settings into the form with a saved baseline and titles the tab from them', async () => {
    asAdmin()
    api.adminGetViewerSettings.mockResolvedValue({ site_title: 'Atlas', max_index_items: 300 })
    mount()
    await screen.findByText('delete Liver')
    fireEvent.click(await waitFor(() => tabButton('Settings')!))
    await waitFor(() => expect(screen.getByTestId('vs-form').textContent).toBe('Atlas|300|baseline'))
    expect(document.title).toBe('Dashboard: Atlas')
  })

  it('leaves the form without a baseline (so unsaveable) when the load fails', async () => {
    asAdmin()
    api.adminGetViewerSettings.mockRejectedValue(new RateLimitError())
    mount()
    await screen.findByText('delete Liver')
    fireEvent.click(await waitFor(() => tabButton('Settings')!))
    expect(screen.getByTestId('vs-form').textContent).toBe('|2000|none')
  })
})

describe('AdminDashboard metadata refresh after slide changes', () => {
  async function openMetadataTab() {
    mount()
    await screen.findByText('delete Liver')
    fireEvent.click(await waitFor(() => tabButton('Metadata')!))
    expect(screen.getByTestId('meta-signal').textContent).toBe('0')
  }

  it('re-counts after a slide delete', async () => {
    api.adminDeleteSlide.mockResolvedValue(undefined)
    await openMetadataTab()
    fireEvent.click(screen.getByText('delete Liver'))
    await waitFor(() => expect(screen.getByTestId('meta-signal').textContent).toBe('1'))
  })

  it('re-counts after a bulk tour change', async () => {
    api.adminListAllTours.mockResolvedValue([{
      id: 10, public_id: 't10', slide_public_id: 'p1', slide_id: 1, title: 'Tour 10',
      description: '', is_published: false, unlisted: false, revision: 3,
    }])
    api.adminBulkTourVisibility.mockResolvedValue({
      ok: true, results: [{ tour_id: 10, ok: true, changed: true, error: null, is_published: false, unlisted: false, revision: 4 }],
    })
    await openMetadataTab()
    fireEvent.click(await screen.findByText('select tour 10'))
    fireEvent.click(within(screen.getByRole('group', { name: /tour\(s\) selected/ })).getByRole('button', { name: 'Draft' }))
    await waitFor(() => expect(screen.getByTestId('meta-signal').textContent).toBe('1'))
  })

  it('re-counts after a bulk delete', async () => {
    vi.stubGlobal('alert', vi.fn())
    api.adminBulkDeleteSlides.mockResolvedValue({ ok: true, deleted: [1], not_found: [], failed: [] })
    await openMetadataTab()
    fireEvent.click(screen.getByText('select Liver'))
    fireEvent.click(await screen.findByRole('button', { name: /^Delete$/ }))
    await waitFor(() => expect(screen.getByTestId('meta-signal').textContent).toBe('1'))
  })

  it('re-counts after a bulk publish', async () => {
    api.adminBulkPublishSlides.mockResolvedValue({
      ok: true, results: [{ slide_id: 1, ok: true, is_published: true, cascaded_tours: 0, error: null }],
    })
    await openMetadataTab()
    fireEvent.click(screen.getByText('select Liver'))
    fireEvent.click(await screen.findByRole('button', { name: /^Publish$/ }))
    await waitFor(() => expect(screen.getByTestId('meta-signal').textContent).toBe('1'))
  })

  it('re-counts after a slide save even when the list refresh fails', async () => {
    await openMetadataTab()
    fireEvent.click(screen.getByText('edit Liver'))
    api.adminListSlides.mockRejectedValue(new RateLimitError())
    fireEvent.click(await screen.findByText('save edit'))
    await waitFor(() => expect(screen.getByTestId('meta-signal').textContent).toBe('1'))
  })

  it('refreshes the slides and metadata options after an import, tolerating a failed refresh', async () => {
    asAdmin()
    mount()
    await screen.findByText('delete Liver')
    fireEvent.click(await waitFor(() => tabButton('Import / Export')!))
    const before = api.adminListMetadataOptions.mock.calls.length
    api.adminListSlides.mockRejectedValue(new RateLimitError())
    fireEvent.click(screen.getByText('imported'))
    await waitFor(() => expect(api.adminListMetadataOptions.mock.calls.length).toBe(before + 1))
    expect(api.adminListSlides).toHaveBeenCalledTimes(2)
    // The rejected refresh is handled (an unhandled rejection fails the run).
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
  })
})
