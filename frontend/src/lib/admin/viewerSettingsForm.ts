import { useCallback, useMemo, useState, type Dispatch, type SetStateAction } from 'react'
import { INDEX_TOURS_PER_SLIDE_DEFAULT, type AdminViewerSettings, type IndexTypeFilter } from '../api'

// The Settings tab's viewer-settings form. The dashboard holds it (through
// useViewerSettingsForm) rather than AdminViewerSettingsSection, so edits and
// the "Unsaved changes" baseline survive sidebar tab switches that unmount the
// section.

// The fields the main "Save viewer settings" button persists, and so the ones
// its dirty indicator tracks.
export type ViewerSettingsValues = {
  copyrightMsg: string
  siteTitle: string
  footerText: string
  showZoomIndicator: boolean
  showScaleBar: boolean
  showMetadataPanel: boolean
  defaultIndexType: IndexTypeFilter
  showFilterButton: boolean
  defaultViewMode: 'tile' | 'list'
  defaultSortField: 'title' | 'catalog_id' | 'wsi_filename'
  filterSortOrder: 'count' | 'alpha'
  defaultThumbnailSize: 'sm' | 'md' | 'lg'
  maxIndexItems: number
  indexToursPerSlide: number
  maxZoomPixelRatio: number
  immediateRender: boolean
  maxImageCacheCount: number
  showAdminLink: boolean
  headerDefaultTitleColor: string
}

export type ViewerSettingsForm = ViewerSettingsValues & {
  // Logo / favicon / header-background image URLs: each is persisted by its
  // own upload button, so they stay out of Save and of the dirty check.
  logoUrl: string
  faviconUrl: string
  headerDefaultImageUrl: string
}

// What the form shows before the server's settings arrive (and keeps showing,
// with Save disabled, if they never do).
export const VIEWER_SETTINGS_FORM_DEFAULTS: ViewerSettingsForm = {
  copyrightMsg: '',
  siteTitle: '',
  footerText: '',
  showZoomIndicator: false,
  showScaleBar: true,
  showMetadataPanel: true,
  defaultIndexType: 'all',
  showFilterButton: false,
  defaultViewMode: 'tile',
  defaultSortField: 'catalog_id',
  filterSortOrder: 'count',
  defaultThumbnailSize: 'lg',
  maxIndexItems: 2000,
  indexToursPerSlide: INDEX_TOURS_PER_SLIDE_DEFAULT,
  maxZoomPixelRatio: 1.0,
  immediateRender: true,
  maxImageCacheCount: 200,
  showAdminLink: false,
  headerDefaultTitleColor: '',
  logoUrl: '',
  faviconUrl: '',
  headerDefaultImageUrl: '',
}

// The form's values for the settings the server sent, a missing or empty
// field taking its default.
export function viewerSettingsFormFrom(vs: AdminViewerSettings): ViewerSettingsForm {
  return {
    copyrightMsg: vs.copyright_msg || '',
    siteTitle: vs.site_title || '',
    footerText: vs.footer_text || '',
    showZoomIndicator: !!vs.show_zoom_indicator,
    showScaleBar: vs.show_scale_bar ?? true,
    showMetadataPanel: vs.show_metadata_panel ?? true,
    defaultIndexType: (vs.default_index_type as IndexTypeFilter) || 'all',
    showFilterButton: vs.show_filter_button ?? false,
    defaultViewMode: vs.default_view_mode || 'tile',
    defaultSortField: vs.default_sort_field || 'catalog_id',
    filterSortOrder: vs.filter_sort_order === 'alpha' ? 'alpha' : 'count',
    defaultThumbnailSize: (vs.default_thumbnail_size as 'sm' | 'md' | 'lg') || 'lg',
    maxIndexItems: typeof vs.max_index_items === 'number' ? vs.max_index_items : 2000,
    // Typeof-guarded rather than `|| default`: 0 is a legitimate value for
    // this one ("list no tours inline until the visitor expands the slide"),
    // and a truthiness check would rewrite it to the default on every load.
    indexToursPerSlide: typeof vs.index_tours_per_slide === 'number'
      ? vs.index_tours_per_slide
      : INDEX_TOURS_PER_SLIDE_DEFAULT,
    maxZoomPixelRatio: typeof vs.max_zoom_pixel_ratio === 'number' ? vs.max_zoom_pixel_ratio : 1.0,
    immediateRender: vs.immediate_render ?? true,
    maxImageCacheCount: typeof vs.max_image_cache_count === 'number' ? vs.max_image_cache_count : 200,
    showAdminLink: !!vs.show_admin_link,
    headerDefaultTitleColor: vs.header_default_title_color || '',
    logoUrl: vs.logo_url || '',
    faviconUrl: vs.favicon_url || '',
    headerDefaultImageUrl: vs.header_default_image_url || '',
  }
}

// The dirty-state comparison key: the saved fields only (an image URL in `v`
// is ignored), in a fixed order. The baseline from the server fetch, the live
// form and the post-save baseline all go through here, so a field added to
// ViewerSettingsValues is compared everywhere at once.
export function viewerSettingsSnapshot(v: ViewerSettingsValues): string {
  return JSON.stringify({
    copyrightMsg: v.copyrightMsg,
    siteTitle: v.siteTitle,
    footerText: v.footerText,
    showZoomIndicator: v.showZoomIndicator,
    showScaleBar: v.showScaleBar,
    showMetadataPanel: v.showMetadataPanel,
    defaultIndexType: v.defaultIndexType,
    showFilterButton: v.showFilterButton,
    defaultViewMode: v.defaultViewMode,
    defaultSortField: v.defaultSortField,
    filterSortOrder: v.filterSortOrder,
    defaultThumbnailSize: v.defaultThumbnailSize,
    maxIndexItems: v.maxIndexItems,
    indexToursPerSlide: v.indexToursPerSlide,
    maxZoomPixelRatio: v.maxZoomPixelRatio,
    immediateRender: v.immediateRender,
    maxImageCacheCount: v.maxImageCacheCount,
    showAdminLink: v.showAdminLink,
    headerDefaultTitleColor: v.headerDefaultTitleColor,
  })
}

export type ViewerSettingsFormApi = {
  values: ViewerSettingsForm
  set: <K extends keyof ViewerSettingsForm>(key: K, value: ViewerSettingsForm[K]) => void
  // viewerSettingsSnapshot of what the server last persisted; '' until the
  // settings have loaded, while nothing can be called dirty (or saved).
  savedSnapshot: string
  setSavedSnapshot: Dispatch<SetStateAction<string>>
  // Show settings the server sent, and take them as the saved baseline.
  load: (values: ViewerSettingsForm) => void
}

export function useViewerSettingsForm(): ViewerSettingsFormApi {
  const [values, setValues] = useState<ViewerSettingsForm>(VIEWER_SETTINGS_FORM_DEFAULTS)
  const [savedSnapshot, setSavedSnapshot] = useState('')
  const set = useCallback(<K extends keyof ViewerSettingsForm>(key: K, value: ViewerSettingsForm[K]) => {
    setValues((prev) => (Object.is(prev[key], value) ? prev : { ...prev, [key]: value }))
  }, [])
  const load = useCallback((next: ViewerSettingsForm) => {
    setValues(next)
    setSavedSnapshot(viewerSettingsSnapshot(next))
  }, [])
  return useMemo(
    () => ({ values, set, savedSnapshot, setSavedSnapshot, load }),
    [values, set, savedSnapshot, load],
  )
}
