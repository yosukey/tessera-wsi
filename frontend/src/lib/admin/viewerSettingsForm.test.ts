// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { AdminViewerSettings } from '../api'
import {
  VIEWER_SETTINGS_FORM_DEFAULTS,
  useViewerSettingsForm,
  viewerSettingsFormFrom,
  viewerSettingsSnapshot,
} from './viewerSettingsForm'

const SERVER: AdminViewerSettings = {
  logo_url: '/assets/logo.png',
  favicon_url: '/assets/favicon.ico',
  copyright_msg: '© Lab',
  extra_attrs: ['stain'],
  index_card_attrs: [],
  index_filter_attrs: [],
  site_title: 'Atlas',
  footer_text: 'Footer',
  show_zoom_indicator: true,
  show_scale_bar: false,
  show_metadata_panel: false,
  default_index_type: 'tours',
  show_filter_button: true,
  default_view_mode: 'list',
  default_sort_field: 'title',
  filter_sort_order: 'alpha',
  max_zoom_pixel_ratio: 1.5,
  show_admin_link: true,
  default_thumbnail_size: 'sm',
  max_index_items: 300,
  index_tours_per_slide: 0,
  immediate_render: false,
  max_image_cache_count: 400,
  header_default_image_url: '/assets/header.png',
  header_default_title_color: '#112233',
}

describe('viewerSettingsFormFrom', () => {
  it('takes every value the server sent, including a 0 tour cap', () => {
    expect(viewerSettingsFormFrom(SERVER)).toEqual({
      copyrightMsg: '© Lab',
      siteTitle: 'Atlas',
      footerText: 'Footer',
      showZoomIndicator: true,
      showScaleBar: false,
      showMetadataPanel: false,
      defaultIndexType: 'tours',
      showFilterButton: true,
      defaultViewMode: 'list',
      defaultSortField: 'title',
      filterSortOrder: 'alpha',
      defaultThumbnailSize: 'sm',
      maxIndexItems: 300,
      indexToursPerSlide: 0,
      maxZoomPixelRatio: 1.5,
      immediateRender: false,
      maxImageCacheCount: 400,
      showAdminLink: true,
      headerDefaultTitleColor: '#112233',
      logoUrl: '/assets/logo.png',
      faviconUrl: '/assets/favicon.ico',
      headerDefaultImageUrl: '/assets/header.png',
    })
  })

  it('falls back to the defaults for missing fields', () => {
    expect(viewerSettingsFormFrom({} as AdminViewerSettings)).toEqual(VIEWER_SETTINGS_FORM_DEFAULTS)
    expect(viewerSettingsFormFrom({ filter_sort_order: 'bogus' } as unknown as AdminViewerSettings).filterSortOrder)
      .toBe('count')
  })
})

describe('viewerSettingsSnapshot', () => {
  it('ignores the image URLs, which have their own upload buttons', () => {
    const form = viewerSettingsFormFrom(SERVER)
    expect(viewerSettingsSnapshot({ ...form, logoUrl: '', faviconUrl: '', headerDefaultImageUrl: '' } as typeof form))
      .toBe(viewerSettingsSnapshot(form))
    expect(viewerSettingsSnapshot({ ...form, siteTitle: 'Other' })).not.toBe(viewerSettingsSnapshot(form))
  })

  it('does not depend on the key order of its input', () => {
    const form = viewerSettingsFormFrom(SERVER)
    const reversed = Object.fromEntries(Object.entries(form).reverse()) as typeof form
    expect(viewerSettingsSnapshot(reversed)).toBe(viewerSettingsSnapshot(form))
  })
})

describe('useViewerSettingsForm', () => {
  it('has no baseline until loaded, then takes the loaded values as saved', () => {
    const { result } = renderHook(() => useViewerSettingsForm())
    expect(result.current.values).toEqual(VIEWER_SETTINGS_FORM_DEFAULTS)
    expect(result.current.savedSnapshot).toBe('')
    const loaded = viewerSettingsFormFrom(SERVER)
    act(() => result.current.load(loaded))
    expect(result.current.values).toEqual(loaded)
    expect(result.current.savedSnapshot).toBe(viewerSettingsSnapshot(loaded))
  })

  it('sets one field at a time, keeping the setter stable', () => {
    const { result } = renderHook(() => useViewerSettingsForm())
    const { set } = result.current
    act(() => result.current.set('siteTitle', 'Atlas'))
    act(() => result.current.set('maxIndexItems', 500))
    expect(result.current.values).toMatchObject({ siteTitle: 'Atlas', maxIndexItems: 500, footerText: '' })
    expect(result.current.set).toBe(set)
    // Setting a field to its current value changes nothing.
    const before = result.current.values
    act(() => result.current.set('siteTitle', 'Atlas'))
    expect(result.current.values).toBe(before)
  })
})
