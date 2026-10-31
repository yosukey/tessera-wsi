// @vitest-environment jsdom
import React, { useEffect } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminUpdateViewerSettings: vi.fn(),
  adminResetHeaderDefault: vi.fn(),
  adminUploadViewerLogo: vi.fn(),
  adminUploadHeaderDefaultBackground: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})
// Its own self-contained section (it fetches the seasonal slots itself).
vi.mock('./AdminSeasonalBackgroundsSection', () => ({ default: () => null }))

import { I18nProvider } from '../../lib/i18n'
import type { AdminViewerSettings } from '../../lib/api'
import { useViewerSettingsForm, viewerSettingsFormFrom } from '../../lib/admin/viewerSettingsForm'
import AdminViewerSettingsSection from './AdminViewerSettingsSection'

const SERVER = {
  logo_url: '', favicon_url: '', copyright_msg: '© Lab', extra_attrs: [], index_card_attrs: [],
  index_filter_attrs: [], site_title: 'Atlas', footer_text: 'Footer', show_zoom_indicator: false,
  show_scale_bar: true, show_metadata_panel: true, default_index_type: 'all', show_filter_button: false,
  default_view_mode: 'tile', default_sort_field: 'catalog_id', filter_sort_order: 'count',
  max_zoom_pixel_ratio: 1.0, show_admin_link: false, default_thumbnail_size: 'lg', max_index_items: 2000,
  index_tours_per_slide: 3, immediate_render: true, max_image_cache_count: 200,
  header_default_image_url: '', header_default_title_color: '#112233',
} as AdminViewerSettings

// The dashboard's side: it owns the form and loads the server's settings.
function Harness(props: {
  loaded?: boolean
  onSavedSiteTitle?: (v: string) => void
  onSavedOverlays?: (v: unknown) => void
  loadError?: boolean
  onRetryLoad?: () => void
}) {
  const form = useViewerSettingsForm()
  const { load } = form
  useEffect(() => { if (props.loaded !== false) load(viewerSettingsFormFrom(SERVER)) }, [load, props.loaded])
  return (
    <AdminViewerSettingsSection
      form={form}
      onSavedSiteTitle={props.onSavedSiteTitle}
      onSavedOverlays={props.onSavedOverlays}
      loadError={props.loadError ?? false}
      onRetryLoad={props.onRetryLoad ?? (() => {})}
    />
  )
}

function mount(props: React.ComponentProps<typeof Harness> = {}) {
  return render(<I18nProvider><Harness {...props} /></I18nProvider>)
}

const saveButton = () => screen.getByRole('button', { name: 'Save settings' }) as HTMLButtonElement
const unsaved = () => screen.queryByText('Unsaved changes')
const siteTitleInput = () => screen.getByDisplayValue('Atlas') as HTMLInputElement
const colorText = () => screen.getByPlaceholderText('#1a2b3c') as HTMLInputElement

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AdminViewerSettingsSection dirty tracking', () => {
  it('is clean after loading, dirty after an edit, and clean again once reverted', async () => {
    mount()
    const input = await waitFor(siteTitleInput)
    expect(unsaved()).toBeNull()
    expect(saveButton().disabled).toBe(true)
    fireEvent.change(input, { target: { value: 'Atlas 2' } })
    expect(unsaved()).not.toBeNull()
    expect(saveButton().disabled).toBe(false)
    fireEvent.change(input, { target: { value: 'Atlas' } })
    expect(unsaved()).toBeNull()
  })

  it('counts a typed-but-not-blurred number by its clamped value', async () => {
    mount()
    await waitFor(siteTitleInput)
    const zoom = screen.getByDisplayValue('1.0')
    fireEvent.change(zoom, { target: { value: '1.04' } }) // rounds to 1.0: unchanged
    expect(unsaved()).toBeNull()
    fireEvent.change(zoom, { target: { value: '1.46' } })
    expect(unsaved()).not.toBeNull()
  })

  it('locks the fields until the settings have loaded', async () => {
    // An edit made before the load lands would be overwritten by it.
    const { rerender } = mount({ loaded: false })
    const title = screen.getByPlaceholderText('Tessera') as HTMLInputElement
    expect(title.closest('fieldset')!.disabled).toBe(true)
    rerender(<I18nProvider><Harness /></I18nProvider>)
    await waitFor(() => expect(title.closest('fieldset')!.disabled).toBe(false))
  })

  it('cannot be saved before the settings have loaded', () => {
    mount({ loaded: false, loadError: true })
    fireEvent.change(screen.getByPlaceholderText('Tessera'), { target: { value: 'X' } })
    expect(unsaved()).toBeNull()
    expect(saveButton().disabled).toBe(true)
    expect(screen.getByRole('alert').textContent).toMatch(/Failed to load viewer settings/)
  })
})

describe('AdminViewerSettingsSection save', () => {
  it('sends every field, numbers normalised and the colour lowercased, then reports and clears', async () => {
    api.adminUpdateViewerSettings.mockResolvedValue({})
    const onSavedSiteTitle = vi.fn()
    const onSavedOverlays = vi.fn()
    mount({ onSavedSiteTitle, onSavedOverlays })
    fireEvent.change(await waitFor(siteTitleInput), { target: { value: 'Atlas 2' } })
    fireEvent.change(screen.getByDisplayValue('1.0'), { target: { value: '1.46' } })
    fireEvent.change(screen.getByDisplayValue('2000'), { target: { value: '99999' } })
    fireEvent.change(colorText(), { target: { value: '#ABCDEF' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /scale bar/i }))
    fireEvent.click(saveButton())
    await waitFor(() => expect(api.adminUpdateViewerSettings).toHaveBeenCalledTimes(1))
    expect(api.adminUpdateViewerSettings).toHaveBeenCalledWith({
      copyright_msg: '© Lab',
      site_title: 'Atlas 2',
      footer_text: 'Footer',
      show_zoom_indicator: false,
      show_scale_bar: false,
      show_metadata_panel: true,
      default_index_type: 'all',
      show_filter_button: false,
      default_view_mode: 'tile',
      default_sort_field: 'catalog_id',
      filter_sort_order: 'count',
      max_zoom_pixel_ratio: 1.5,
      default_thumbnail_size: 'lg',
      max_index_items: 5000,
      index_tours_per_slide: 3,
      immediate_render: true,
      max_image_cache_count: 200,
      show_admin_link: false,
      header_default_title_color: '#abcdef',
    })
    await screen.findByText('Saved')
    expect(onSavedSiteTitle).toHaveBeenCalledWith('Atlas 2')
    expect(onSavedOverlays).toHaveBeenCalledWith({ showZoomIndicator: false, showScaleBar: false, showMetadataPanel: true })
    // The baseline is what was sent, so the normalised fields read clean.
    expect(unsaved()).toBeNull()
    expect(screen.getByDisplayValue('1.5')).toBeTruthy()
    expect(screen.getByDisplayValue('5000')).toBeTruthy()
    expect(colorText().value).toBe('#abcdef')
  })

  it('keeps the edits and the unsaved state when the save fails', async () => {
    api.adminUpdateViewerSettings.mockRejectedValue(new Error('disk full'))
    mount()
    fireEvent.change(await waitFor(siteTitleInput), { target: { value: 'Atlas 2' } })
    fireEvent.click(saveButton())
    await screen.findByText('Save failed: disk full')
    expect(unsaved()).not.toBeNull()
    expect(screen.getByDisplayValue('Atlas 2')).toBeTruthy()
  })

  it('a header reset clears the saved colour without hiding other unsaved edits', async () => {
    api.adminResetHeaderDefault.mockResolvedValue({})
    mount()
    await waitFor(siteTitleInput)
    fireEvent.click(screen.getByRole('button', { name: 'Reset default header' }))
    await waitFor(() => expect(api.adminResetHeaderDefault).toHaveBeenCalled())
    await waitFor(() => expect(colorText().value).toBe(''))
    // Only the colour changed, server-side too: still clean.
    expect(unsaved()).toBeNull()
    fireEvent.change(siteTitleInput(), { target: { value: 'Atlas 2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reset default header' }))
    await waitFor(() => expect(api.adminResetHeaderDefault).toHaveBeenCalledTimes(2))
    expect(unsaved()).not.toBeNull()
  })

  it("locks the header default's fields while its reset is in flight", async () => {
    // The reset clears the colour and its baseline, so a colour typed after
    // the click would be wiped and read as saved.
    let finish: (v: unknown) => void = () => {}
    api.adminResetHeaderDefault.mockReturnValue(new Promise((r) => { finish = r }))
    mount()
    await waitFor(siteTitleInput)
    const reset = screen.getByRole('button', { name: 'Reset default header' }) as HTMLButtonElement
    fireEvent.click(reset)
    await waitFor(() => expect(colorText().closest('fieldset')!.disabled).toBe(true))
    expect(reset.closest('fieldset')).toBe(colorText().closest('fieldset'))
    // A repeat click sends nothing more.
    fireEvent.click(reset)
    expect(api.adminResetHeaderDefault).toHaveBeenCalledTimes(1)
    await act(async () => finish({}))
    expect(colorText().closest('fieldset')!.disabled).toBe(false)
    expect(colorText().value).toBe('')
  })

  it("doesn't let a save and a header reset overlap", async () => {
    // Each one's response rewrites the baseline the other relies on.
    let finishSave: (v: unknown) => void = () => {}
    api.adminUpdateViewerSettings.mockReturnValue(new Promise((r) => { finishSave = r }))
    mount()
    const input = await waitFor(siteTitleInput)
    fireEvent.change(input, { target: { value: 'Atlas 2' } })
    fireEvent.click(saveButton())
    const reset = screen.getByRole('button', { name: 'Reset default header' }) as HTMLButtonElement
    await waitFor(() => expect(reset.disabled).toBe(true))
    await act(async () => finishSave({}))
    expect(reset.disabled).toBe(false)
    api.adminResetHeaderDefault.mockReturnValue(new Promise(() => {}))
    fireEvent.change(input, { target: { value: 'Atlas 3' } })
    fireEvent.click(reset)
    await waitFor(() => expect(saveButton().disabled).toBe(true))
  })
})

describe('AdminViewerSettingsSection images', () => {
  it("locks an image's row while its upload is out", async () => {
    // A second pick could otherwise land first and be overwritten.
    api.adminUploadViewerLogo.mockReturnValue(new Promise(() => {}))
    mount()
    await waitFor(siteTitleInput)
    const logo = document.querySelector('input[type=file][accept*=".webp"]') as HTMLInputElement
    fireEvent.change(logo, { target: { files: [new File(['x'], 'logo.png', { type: 'image/png' })] } })
    await waitFor(() => expect(logo.closest('fieldset')!.disabled).toBe(true))
    // Only that row: the title color stays editable.
    expect(colorText().closest('fieldset')!.disabled).toBe(false)
  })

  it("keeps the header default's Reset apart from its image upload", async () => {
    // Reset clears the image the upload would set.
    api.adminUploadHeaderDefaultBackground.mockReturnValue(new Promise(() => {}))
    mount()
    await waitFor(siteTitleInput)
    const header = document.querySelector('input[type=file][accept*=".apng"]') as HTMLInputElement
    fireEvent.change(header, { target: { files: [new File(['x'], 'bg.png', { type: 'image/png' })] } })
    const reset = screen.getByRole('button', { name: 'Reset default header' }) as HTMLButtonElement
    await waitFor(() => expect(reset.disabled).toBe(true))
  })
})
