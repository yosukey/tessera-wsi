// @vitest-environment jsdom
import React from 'react'
import { MemoryRouter } from 'react-router'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminPublishSlide: vi.fn(),
  adminSetSlideUnlisted: vi.fn(),
  isManagerOrAboveFromToken: vi.fn(() => true),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})
vi.mock('./UrlShareControl', () => ({ default: () => null }))

import { I18nProvider } from '../../lib/i18n'
import type { Slide, TourSummary } from '../../lib/api'
import SlideList from './SlideList'

const SLIDE = {
  id: 7, public_id: 'p7', catalog_id: 'C-7', title: 'Liver', thumb_url: '', width: 1, height: 1,
  mpp_x: 0.25, mpp_y: 0.25, vendor: 'aperio', wsi_filename: '7_liver.svs', attributes: {},
  is_published: true, unlisted: false,
  show_zoom_indicator: null, show_scale_bar: null, show_metadata_panel: null,
} as Slide

const TOUR: TourSummary = {
  id: 5, public_id: 't5', slide_public_id: 'p7', slide_id: 7, title: 'Lobule', description: '',
  is_published: false, unlisted: false, revision: 0,
}

function mount(
  onConfirmUnpublish: (id: number) => Promise<boolean>,
  slideTours: Record<number, TourSummary[]> = {},
  selectedTours: Set<number> = new Set(),
) {
  const props = {
    onConfirmUnpublish: vi.fn(onConfirmUnpublish),
    onToggleSelectTour: vi.fn(),
    onToursDrafted: vi.fn(),
    onPatchSlideLocal: vi.fn(),
    onLinkClick: vi.fn(),
    onDuplicateTour: vi.fn(),
  }
  render(
    <MemoryRouter>
      <I18nProvider>
        <SlideList
          slides={[SLIDE]}
          slideTours={slideTours}
          selected={new Set()}
          onToggleSelect={() => {}}
          selectedTours={selectedTours}
          onOpenEdit={() => {}}
          onDeleteSlide={() => {}}
          onCreateTour={() => {}}
          onPatchTourLocal={() => {}}
          {...props}
        />
      </I18nProvider>
    </MemoryRouter>,
  )
  return props
}

const draftButton = () => within(screen.getAllByRole('group', { name: 'Visibility' })[0]).getByRole('button', { name: 'Draft' })

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SlideList per-row unpublish', () => {
  it("asks the dashboard first (which checks the tours itself) and stops when declined", async () => {
    const props = mount(async () => false)
    fireEvent.click(draftButton())
    await waitFor(() => expect(props.onConfirmUnpublish).toHaveBeenCalledWith(7))
    await waitFor(() => expect(draftButton()).not.toHaveProperty('disabled', true))
    expect(api.adminPublishSlide).not.toHaveBeenCalled()
    expect(props.onPatchSlideLocal).not.toHaveBeenCalled()
  })

  it('unpublishes once confirmed and reports the tours the server cascaded', async () => {
    api.adminPublishSlide.mockResolvedValue({ ok: true, is_published: false, cascaded_tours: 2 })
    const props = mount(async () => true)
    fireEvent.click(draftButton())
    await waitFor(() => expect(api.adminPublishSlide).toHaveBeenCalledWith(7, false))
    await waitFor(() => expect(props.onToursDrafted).toHaveBeenCalledWith(7))
    expect(props.onPatchSlideLocal).toHaveBeenCalledWith(7, { is_published: false, unlisted: false })
  })

  it('does not ask when making the slide link-only', async () => {
    api.adminSetSlideUnlisted.mockResolvedValue({ ok: true, unlisted: true })
    const props = mount(async () => true)
    const group = screen.getAllByRole('group', { name: 'Visibility' })[0]
    fireEvent.click(within(group).getByRole('button', { name: /link/i }))
    await waitFor(() => expect(api.adminSetSlideUnlisted).toHaveBeenCalledWith(7, true))
    expect(props.onConfirmUnpublish).not.toHaveBeenCalled()
  })
})

describe('SlideList tour editor links', () => {
  it('hands a click on Edit to the dashboard, which confirms and leaves through its guard', () => {
    const props = mount(async () => true, { 7: [TOUR] })
    const edit = screen.getByRole('link', { name: 'Edit' })
    // Still a real link: a modified click opens the editor in a new tab.
    expect(edit.getAttribute('href')).toBe('/admin/tours/5')
    fireEvent.click(edit)
    expect(props.onLinkClick).toHaveBeenCalledTimes(1)
    expect(props.onLinkClick.mock.calls[0][1]).toBe('/admin/tours/5')
  })
})

describe('SlideList tour duplicate', () => {
  it('hands the tour to the dashboard, which asks for the title', () => {
    const props = mount(async () => true, { 7: [TOUR] })
    fireEvent.click(screen.getByRole('button', { name: 'Duplicate' }))
    expect(props.onDuplicateTour).toHaveBeenCalledWith(TOUR)
  })
})

describe('SlideList tour metadata', () => {
  it("shows a tour's own metadata on its row", () => {
    mount(async () => true, { 7: [{ ...TOUR, attributes: { level: ['basic'], stain: ['PAS'] } }] })
    expect(screen.getByText('PAS · basic')).toBeTruthy()
  })
})

describe('SlideList tour selection', () => {
  it('gives each tour row its own checkbox, reflecting and toggling the tour selection', () => {
    const props = mount(async () => true, { 7: [TOUR, { ...TOUR, id: 6, public_id: 't6', title: 'Portal' }] }, new Set([6]))
    const five = screen.getByRole('checkbox', { name: 'Select tour #5' }) as HTMLInputElement
    const six = screen.getByRole('checkbox', { name: 'Select tour #6' }) as HTMLInputElement
    expect(five.checked).toBe(false)
    expect(six.checked).toBe(true)
    fireEvent.click(five)
    expect(props.onToggleSelectTour).toHaveBeenCalledWith(5)
    // Separate from the slide's own checkbox.
    expect((screen.getByRole('checkbox', { name: 'Select slide #7' }) as HTMLInputElement).checked).toBe(false)
  })
})

describe('SlideList visibility of a slide and its tours', () => {
  it("holds the slide's visibility while one of its tours' is out", async () => {
    // A slide to draft cascades its tours to draft, so the two answers could
    // land in either order (as the tour's already waits for its slide's).
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    mount(async () => true, { 7: [TOUR] })
    const groups = screen.getAllByRole('group', { name: 'Visibility' })
    fireEvent.click(within(groups[1]).getByRole('button', { name: 'Link-only' }))
    await waitFor(() => expect(within(groups[1]).getByRole('button', { name: 'Link-only' })).toHaveProperty('disabled', true))
    expect(draftButton()).toHaveProperty('disabled', true)
    vi.unstubAllGlobals()
  })
})
