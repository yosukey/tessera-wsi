import React, { useEffect, useState } from 'react'
import {
  INDEX_TOURS_PER_SLIDE_MAX,
  INDEX_TOURS_PER_SLIDE_MIN,
  IndexTypeFilter,
  MAX_IMAGE_CACHE_COUNT_DEFAULT,
  MAX_IMAGE_CACHE_COUNT_MAX,
  MAX_IMAGE_CACHE_COUNT_MIN,
  MAX_INDEX_ITEMS_MAX,
  MAX_INDEX_ITEMS_MIN,
  adminDeleteHeaderDefaultBackground,
  adminDeleteViewerFavicon,
  adminDeleteViewerLogo,
  adminResetHeaderDefault,
  adminUpdateViewerSettings,
  adminUploadHeaderDefaultBackground,
  adminUploadViewerFavicon,
  adminUploadViewerLogo,
  clampIndexToursPerSlide,
  clampMaxImageCacheCount,
  clampMaxIndexItems,
  clampMaxZoomPixelRatio,
} from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { apiErrorMessage, failureMessage } from '../../lib/admin/apiError'
import { LockFieldset, Select } from '../ui'
import { UPLOAD_LIMITS, isValidHexColor } from '../../lib/admin/validation'
import {
  viewerSettingsSnapshot,
  type ViewerSettingsFormApi,
  type ViewerSettingsValues,
} from '../../lib/admin/viewerSettingsForm'
import AdminSeasonalBackgroundsSection from './AdminSeasonalBackgroundsSection'
import { SectionMessage, itemRow, subHeading, useSectionMessage } from './settingsSectionUi'
import { cssImageUrl, useAdminBlobUrl } from '../../hooks/useAdminBlobUrl'
import { useInFlight, useInFlightKey } from '../../hooks/useInFlight'

interface Props {
  // The form's values and saved baseline, held by the dashboard so they
  // survive this component unmounting on a sidebar tab switch.
  form: ViewerSettingsFormApi
  // Called with the site title value the server just persisted, so the
  // parent can drive document.title from the saved (not edited) value
  // and stop the tab title from flickering on every keystroke.
  onSavedSiteTitle?: (v: string) => void
  // Called with the three overlay defaults the server just persisted. The
  // slide editor's "inherited" display reads these rather than the live
  // checkboxes above, which may hold unsaved edits.
  onSavedOverlays?: (v: { showZoomIndicator: boolean; showScaleBar: boolean; showMetadataPanel: boolean }) => void
  // True when the dashboard's initial viewer-settings fetch failed.
  // The Save button is already implicitly disabled in that state
  // (the saved snapshot stays '' so isDirty is false), but without an
  // explicit banner the admin sees fields filled with default values
  // and no hint that those aren't what the server actually has.
  loadError: boolean
  onRetryLoad: () => void
}

// Parse a numeric field's typed text into its clamped value and the text to
// show for it. Unparseable text keeps `fallback` (the current canonical value),
// so an interrupted edit reverts on blur instead of snapping to a default.
function normaliseNumberInput(
  rawText: string,
  fallback: number,
  clamp: (n: number) => number,
  format: (n: number) => string = String,
): { value: number; display: string } {
  const parsed = parseFloat(rawText)
  const value = isFinite(parsed) ? clamp(parsed) : fallback
  return { value, display: format(value) }
}

const formatZoom = (n: number) => n.toFixed(1)

export default function AdminViewerSettingsSection({
  form,
  onSavedSiteTitle,
  onSavedOverlays,
  loadError, onRetryLoad,
}: Props) {
  const { t } = useI18n()
  const { values: v, set, savedSnapshot, setSavedSnapshot } = form
  const { msg: vsMsg, setMsg: setVsMsg, clearMsg: clearVsMsg } = useSectionMessage()
  const logoPreview = useAdminBlobUrl(v.logoUrl)
  const faviconPreview = useAdminBlobUrl(v.faviconUrl)
  const headerPreview = useAdminBlobUrl(v.headerDefaultImageUrl)
  // The Save and the header default's Reset share one slot: each rewrites
  // the baseline the other relies on, so they never overlap.
  const request = useInFlightKey<'save' | 'reset'>()
  const vsSaving = request.key === 'save'
  // The settings haven't loaded yet (or failed to): savedSnapshot is set by
  // the load.
  const notLoaded = !savedSnapshot
  // The header default's Reset in flight. Its fields are locked meanwhile:
  // the response clears them (and their baseline), so a color typed after
  // the click would be wiped and look saved.
  const headerResetting = request.key === 'reset'
  // Each image's upload / removal: its row is locked until the answer lands,
  // so a second pick can't finish first and be overwritten by the first.
  const logoRequest = useInFlight()
  const faviconRequest = useInFlight()
  const headerImageRequest = useInFlight()
  // Track the raw text the user has typed for the max-zoom field separately
  // from the canonical numeric state. Clamping/rounding on every keystroke
  // would make free typing impossible — typing "1.5"
  // would get eaten as "1" → "1.0" → "1.05" → rounded "1.1" without ever
  // landing on the intended value. Normalisation happens only on blur.
  const [maxZoomRaw, setMaxZoomRaw] = useState(() => v.maxZoomPixelRatio.toFixed(1))
  useEffect(() => {
    setMaxZoomRaw(v.maxZoomPixelRatio.toFixed(1))
  }, [v.maxZoomPixelRatio])
  // Same raw-vs-canonical split as maxZoom: clamping on every keystroke
  // makes typing into the field awkward (e.g. typing "1500" gets eaten to
  // "100"/"500" mid-stroke). Canonicalise on blur / save instead.
  const [maxIndexItemsRaw, setMaxIndexItemsRaw] = useState(() => String(v.maxIndexItems))
  useEffect(() => {
    setMaxIndexItemsRaw(String(v.maxIndexItems))
  }, [v.maxIndexItems])
  const commitMaxIndexItems = () => {
    const { value, display } = normaliseNumberInput(maxIndexItemsRaw, v.maxIndexItems, clampMaxIndexItems)
    set('maxIndexItems', value)
    setMaxIndexItemsRaw(display)
  }
  // Same raw-vs-canonical split for the per-slide inline tour cap. Note the
  // valid range starts at 0, so an empty field must NOT fall back to the
  // default — parseInt('') is NaN and the fallback is the current canonical
  // value, which keeps "0" typed-then-cleared from silently becoming 3.
  const [indexToursPerSlideRaw, setIndexToursPerSlideRaw] = useState(() => String(v.indexToursPerSlide))
  useEffect(() => {
    setIndexToursPerSlideRaw(String(v.indexToursPerSlide))
  }, [v.indexToursPerSlide])
  const commitIndexToursPerSlide = () => {
    const { value, display } = normaliseNumberInput(indexToursPerSlideRaw, v.indexToursPerSlide, clampIndexToursPerSlide)
    set('indexToursPerSlide', value)
    setIndexToursPerSlideRaw(display)
  }
  // Same raw-vs-canonical split for the advanced tile-cache count: clamp on
  // blur / save, not per keystroke, so typing "1500" isn't eaten to "500"
  // mid-stroke. The server re-clamps anyway, but the GUI is the guardrail.
  const [maxImageCacheCountRaw, setMaxImageCacheCountRaw] = useState(() => String(v.maxImageCacheCount))
  useEffect(() => {
    setMaxImageCacheCountRaw(String(v.maxImageCacheCount))
  }, [v.maxImageCacheCount])
  const commitMaxImageCacheCount = () => {
    const { value, display } = normaliseNumberInput(maxImageCacheCountRaw, v.maxImageCacheCount, clampMaxImageCacheCount)
    set('maxImageCacheCount', value)
    setMaxImageCacheCountRaw(display)
  }
  const resetMaxImageCacheCount = () => {
    set('maxImageCacheCount', MAX_IMAGE_CACHE_COUNT_DEFAULT)
    setMaxImageCacheCountRaw(String(MAX_IMAGE_CACHE_COUNT_DEFAULT))
  }
  // Normalise `maxZoomRaw` (the user-typed text) into a clamped number,
  // returning both the canonical value and the formatted display string.
  // Used by both the input's onBlur handler and the save handler — the
  // save path can't rely on the onBlur having already fired (e.g. Enter
  // submits before blur in some flows, and React batches state updates
  // so a fresh setVsMaxZoomPixelRatio would not be visible in the same
  // tick's save closure).
  const normaliseMaxZoom = (rawText: string, fallback: number) =>
    normaliseNumberInput(rawText, fallback, clampMaxZoomPixelRatio, formatZoom)
  const commitMaxZoom = () => {
    const { value, display } = normaliseMaxZoom(maxZoomRaw, v.maxZoomPixelRatio)
    set('maxZoomPixelRatio', value)
    setMaxZoomRaw(display)
  }

  // Snapshot of all fields the main "Save viewer settings" button
  // persists. Logo / favicon URLs are deliberately excluded because
  // they're saved by their own upload buttons and shouldn't make the
  // main save look dirty. maxZoomRaw is fed through normaliseMaxZoom so
  // a typed-but-not-yet-blurred value still flags the form as dirty
  // (the save handler would persist that normalised value).
  // (extra_attrs / orphan-key handling lives in AdminMetadataSection, not
  // here — this snapshot covers only the viewer-display settings below.)
  const currentSnapshot = viewerSettingsSnapshot({
    ...v,
    maxIndexItems: normaliseNumberInput(maxIndexItemsRaw, v.maxIndexItems, clampMaxIndexItems).value,
    indexToursPerSlide: normaliseNumberInput(indexToursPerSlideRaw, v.indexToursPerSlide, clampIndexToursPerSlide).value,
    maxZoomPixelRatio: normaliseMaxZoom(maxZoomRaw, v.maxZoomPixelRatio).value,
    maxImageCacheCount: normaliseNumberInput(maxImageCacheCountRaw, v.maxImageCacheCount, clampMaxImageCacheCount).value,
  })
  // savedSnapshot is '' before the initial fetch finishes; until
  // there's a known-saved baseline to compare against, nothing can be
  // claimed as "dirty", so the indicator is suppressed in that window.
  const isDirty = !!savedSnapshot && currentSnapshot !== savedSnapshot
  // Title color is allowed to be empty (means "use theme default"), but
  // a non-empty value must be a 6-digit hex. Gating Save on this stops
  // the user from posting a payload the server will 422 on, and powers
  // the inline hint below the text input.
  const headerColorInvalid = v.headerDefaultTitleColor !== '' && !isValidHexColor(v.headerDefaultTitleColor)

  function onSaveViewerSettings() {
    return request.run('save', saveViewerSettings)
  }

  async function saveViewerSettings() {
    // Defense in depth: the Save button is also disabled while the
    // header title color is malformed, but a stale closure or a
    // programmatic click could still get here. Surface the validation
    // hint rather than letting the server return 422.
    if (headerColorInvalid) {
      setVsMsg(t('adminDash.invalidColorHex'), 'error')
      return
    }
    clearVsMsg()
    try {
      // Re-parse the user-typed max-zoom value at submit time. Relying on
      // the input's onBlur to update `v.maxZoomPixelRatio` before this
      // handler runs is racy (Enter-to-submit, programmatic clicks, and
      // React's batched state updates can all make the closure here see
      // the pre-edit value). Normalising directly from `maxZoomRaw` here
      // guarantees the saved number matches what the user can see in the
      // input.
      const { value: maxZoomValue, display: maxZoomDisplay } = normaliseMaxZoom(maxZoomRaw, v.maxZoomPixelRatio)
      if (maxZoomValue !== v.maxZoomPixelRatio) set('maxZoomPixelRatio', maxZoomValue)
      if (maxZoomDisplay !== maxZoomRaw) setMaxZoomRaw(maxZoomDisplay)
      // Same flush-on-submit pattern as maxZoom: a typed-but-not-blurred
      // index-cap value would otherwise be lost when the save closure
      // captures the pre-edit state.
      const { value: maxIndexItemsValue, display: maxIndexItemsDisplay } = normaliseNumberInput(maxIndexItemsRaw, v.maxIndexItems, clampMaxIndexItems)
      if (maxIndexItemsValue !== v.maxIndexItems) set('maxIndexItems', maxIndexItemsValue)
      if (maxIndexItemsDisplay !== maxIndexItemsRaw) setMaxIndexItemsRaw(maxIndexItemsDisplay)
      // Same flush-on-submit pattern for the per-slide inline tour cap.
      const { value: indexToursPerSlideValue, display: indexToursPerSlideDisplay } = normaliseNumberInput(indexToursPerSlideRaw, v.indexToursPerSlide, clampIndexToursPerSlide)
      if (indexToursPerSlideValue !== v.indexToursPerSlide) set('indexToursPerSlide', indexToursPerSlideValue)
      if (indexToursPerSlideDisplay !== indexToursPerSlideRaw) setIndexToursPerSlideRaw(indexToursPerSlideDisplay)
      // Same flush-on-submit pattern for the advanced tile-cache count.
      const { value: maxImageCacheCountValue, display: maxImageCacheCountDisplay } = normaliseNumberInput(maxImageCacheCountRaw, v.maxImageCacheCount, clampMaxImageCacheCount)
      if (maxImageCacheCountValue !== v.maxImageCacheCount) set('maxImageCacheCount', maxImageCacheCountValue)
      if (maxImageCacheCountDisplay !== maxImageCacheCountRaw) setMaxImageCacheCountRaw(maxImageCacheCountDisplay)
      // Same idea as max-zoom: flush typed-but-not-blurred title color
      // text to the canonical lowercased form before the request goes
      // out, so the snapshot stamped post-save matches what the server
      // actually stored. headerColorInvalid in the disabled-state of the
      // Save button below already prevents a malformed value reaching
      // this point.
      const headerTitleColor = v.headerDefaultTitleColor.trim().toLowerCase()
      if (headerTitleColor !== v.headerDefaultTitleColor) set('headerDefaultTitleColor', headerTitleColor)
      await adminUpdateViewerSettings({
        copyright_msg: v.copyrightMsg,
        site_title: v.siteTitle,
        footer_text: v.footerText,
        show_zoom_indicator: v.showZoomIndicator,
        show_scale_bar: v.showScaleBar,
        show_metadata_panel: v.showMetadataPanel,
        default_index_type: v.defaultIndexType,
        show_filter_button: v.showFilterButton,
        default_view_mode: v.defaultViewMode,
        default_sort_field: v.defaultSortField,
        filter_sort_order: v.filterSortOrder,
        max_zoom_pixel_ratio: maxZoomValue,
        default_thumbnail_size: v.defaultThumbnailSize,
        max_index_items: maxIndexItemsValue,
        index_tours_per_slide: indexToursPerSlideValue,
        immediate_render: v.immediateRender,
        max_image_cache_count: maxImageCacheCountValue,
        show_admin_link: v.showAdminLink,
        header_default_title_color: headerTitleColor,
      })
      onSavedSiteTitle?.(v.siteTitle)
      onSavedOverlays?.({
        showZoomIndicator: v.showZoomIndicator,
        showScaleBar: v.showScaleBar,
        showMetadataPanel: v.showMetadataPanel,
      })
      // Fresh snapshot reflecting the values that were actually sent (including
      // the normalised maxZoom and the normalised title color) so the dirty
      // indicator clears even if those inputs were typed but not blurred before
      // save.
      setSavedSnapshot(viewerSettingsSnapshot({
        ...v,
        maxIndexItems: maxIndexItemsValue,
        indexToursPerSlide: indexToursPerSlideValue,
        maxZoomPixelRatio: maxZoomValue,
        maxImageCacheCount: maxImageCacheCountValue,
        headerDefaultTitleColor: headerTitleColor,
      }))
      setVsMsg(t('adminDash.saved'), 'success')
    } catch (err) {
      // Preserve the server-side error message instead of dropping it.
      setVsMsg(failureMessage(t('adminDash.saveFailed'), err, t), 'error')
    }
  }

  async function onUploadLogo(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    // Reset the file input early so the same file can be re-selected
    // after a validation failure (browsers suppress `change` for an
    // unchanged value).
    e.target.value = ''
    if (!file) return
    if (file.size > UPLOAD_LIMITS.LOGO.bytes) {
      setVsMsg(t('common.fileTooLarge', { limit: UPLOAD_LIMITS.LOGO.label }), 'error')
      return
    }
    await logoRequest.run(async () => {
      clearVsMsg()
      try {
        const r = await adminUploadViewerLogo(file)
        set('logoUrl', r.logo_url)
        setVsMsg(t('adminDash.logoUploaded'), 'success')
      } catch (err: any) {
        setVsMsg(apiErrorMessage(err, t) || t('adminDash.logoUploadFailed'), 'error')
      }
    })
  }

  async function onDeleteLogo() {
    await logoRequest.run(async () => {
      clearVsMsg()
      try {
        await adminDeleteViewerLogo()
        set('logoUrl', '')
        setVsMsg(t('adminDash.logoRemoved'), 'success')
      } catch {
        setVsMsg(t('adminDash.removeFailed'), 'error')
      }
    })
  }

  async function onUploadFavicon(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    if (file.size > UPLOAD_LIMITS.FAVICON.bytes) {
      setVsMsg(t('common.fileTooLarge', { limit: UPLOAD_LIMITS.FAVICON.label }), 'error')
      return
    }
    await faviconRequest.run(async () => {
      clearVsMsg()
      try {
        const r = await adminUploadViewerFavicon(file)
        set('faviconUrl', r.favicon_url)
        setVsMsg(t('adminDash.faviconUploaded'), 'success')
      } catch (err: any) {
        setVsMsg(apiErrorMessage(err, t) || t('adminDash.faviconUploadFailed'), 'error')
      }
    })
  }

  async function onDeleteFavicon() {
    await faviconRequest.run(async () => {
      clearVsMsg()
      try {
        await adminDeleteViewerFavicon()
        set('faviconUrl', '')
        setVsMsg(t('adminDash.faviconRemoved'), 'success')
      } catch {
        setVsMsg(t('adminDash.removeFailed'), 'error')
      }
    })
  }

  async function onUploadHeaderDefaultBackground(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    if (file.size > UPLOAD_LIMITS.HEADER_IMAGE.bytes) {
      setVsMsg(t('common.fileTooLarge', { limit: UPLOAD_LIMITS.HEADER_IMAGE.label }), 'error')
      return
    }
    await headerImageRequest.run(async () => {
      clearVsMsg()
      try {
        const r = await adminUploadHeaderDefaultBackground(file)
        set('headerDefaultImageUrl', r.header_default_image_url)
        setVsMsg(t('adminDash.headerDefault.imageUploaded'), 'success')
      } catch (err: any) {
        setVsMsg(apiErrorMessage(err, t) || t('adminDash.headerDefault.imageUploadFailed'), 'error')
      }
    })
  }

  // Image-only delete. The section's Reset button below also clears
  // title color in one round-trip; this button is for the (common) case
  // where the admin wants to keep their chosen title color but drop the
  // background pattern.
  async function onDeleteHeaderDefaultBackground() {
    await headerImageRequest.run(async () => {
      clearVsMsg()
      try {
        await adminDeleteHeaderDefaultBackground()
        set('headerDefaultImageUrl', '')
        setVsMsg(t('adminDash.headerDefault.imageRemoved'), 'success')
      } catch {
        setVsMsg(t('adminDash.removeFailed'), 'error')
      }
    })
  }

  // Reset = single round-trip that clears the image AND the title color.
  // Confirmation is intentionally not asked for: a) the action is undoable
  // by re-uploading, and b) parity with the favicon "Revert to default"
  // button elsewhere in this settings section, which is also confirmation-less.
  function onResetHeaderDefault() {
    return request.run('reset', resetHeaderDefault)
  }

  async function resetHeaderDefault() {
    clearVsMsg()
    try {
      await adminResetHeaderDefault()
      set('headerDefaultImageUrl', '')
      set('headerDefaultTitleColor', '')
      // Reset clears the title color server-side; keep the saved snapshot
      // in lockstep so the "Unsaved changes" indicator doesn't spuriously
      // light up. The title color is patched in-place rather than rebuilding
      // the snapshot from current state so any unrelated dirty fields stay
      // dirty. Rebuilding through viewerSettingsSnapshot (rather than
      // JSON.stringify on the patched object) keeps key order pinned to the
      // helper, so future field additions in one place don't desync the
      // other.
      // Patched in the updater, not from the snapshot of the click: a save
      // that landed meanwhile has moved the baseline on.
      setSavedSnapshot((prev) => {
        try {
          const parsed = prev ? JSON.parse(prev) : null
          if (parsed && typeof parsed === 'object') {
            return viewerSettingsSnapshot({
              ...(parsed as ViewerSettingsValues),
              headerDefaultTitleColor: '',
            })
          }
        } catch {
          // If the saved snapshot was malformed for any reason, leave it
          // alone — the worst case is the user has to click Save once.
        }
        return prev
      })
      setVsMsg(t('adminDash.headerDefault.resetDone'), 'success')
    } catch {
      setVsMsg(t('adminDash.removeFailed'), 'error')
    }
  }

  return (
    <>
      <h3 style={{ marginTop: 16 }}>{t('adminDash.viewerDisplaySettings')}</h3>
      {loadError && (
        <div
          role="alert"
          style={{
            marginBottom: 10,
            padding: '8px 10px',
            border: '1px solid var(--color-danger-border)',
            background: 'var(--color-danger-bg, #fee2e2)',
            color: 'var(--color-danger-fg, #991b1b)',
            borderRadius: 5,
            fontSize: 12,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            flexWrap: 'wrap',
          }}
        >
          <span style={{ flex: 1 }}>{t('adminDash.settingsLoadFailed')}</span>
          <button type="button" onClick={onRetryLoad} style={{ fontSize: 11 }}>
            {t('adminDash.retry')}
          </button>
        </div>
      )}
      <div style={{ display: 'grid', gap: 10 }}>

        {/* Until the settings have loaded, a field edited here would be
            overwritten by the load (the seasonal slots load on their own). */}
        <LockFieldset locked={notLoaded} style={{ display: 'grid', gap: 10 }}>
          {/* top page */}
          <div style={{ borderLeft: '3px solid var(--color-border-strong)', paddingLeft: 8, marginTop: 4 }}>
            <div style={subHeading}>{t('adminDash.sectionTopPage')}</div>
            <div style={{ display: 'grid', gap: 6 }}>
              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.siteTitleLabel')}</div>
                <input
                  value={v.siteTitle}
                  onChange={(e) => set('siteTitle', e.target.value)}
                  placeholder="Tessera"
                  // Mirrors the viewer_settings.site_title column width; the
                  // server also caps/strips so this is just an early guard.
                  maxLength={256}
                  style={{ width: '100%', boxSizing: 'border-box' }}
                />
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.siteTitleHelp')}</div>
              </div>

              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.footerTextLabel')}</div>
                <textarea
                  value={v.footerText}
                  onChange={(e) => set('footerText', e.target.value)}
                  placeholder="© 2025 Your Organization. All rights reserved."
                  rows={3}
                  // Mirrors the viewer_settings.footer_text column width.
                  maxLength={1024}
                  style={{ width: '100%', boxSizing: 'border-box', fontSize: 12, resize: 'vertical' }}
                />
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.footerTextHelp')}</div>
              </div>

              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.defaultIndexTypeLabel')}</div>
                <Select
                  size="sm"
                  value={v.defaultIndexType}
                  onChange={(e) => set('defaultIndexType', e.target.value as IndexTypeFilter)}
                >
                  <option value="all">{t('index.type.all')}</option>
                  <option value="slides">{t('index.type.slides')}</option>
                  <option value="tours">{t('index.type.tours')}</option>
                </Select>
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.defaultIndexTypeHelp')}</div>
              </div>

              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.defaultViewModeLabel')}</div>
                <Select
                  size="sm"
                  value={v.defaultViewMode}
                  onChange={(e) => set('defaultViewMode', e.target.value as 'tile' | 'list')}
                >
                  <option value="tile">{t('adminDash.viewModeTile')}</option>
                  <option value="list">{t('adminDash.viewModeList')}</option>
                </Select>
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.defaultViewModeHelp')}</div>
              </div>

              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.defaultThumbnailSizeLabel')}</div>
                <Select
                  size="sm"
                  value={v.defaultThumbnailSize}
                  onChange={(e) => set('defaultThumbnailSize', e.target.value as 'sm' | 'md' | 'lg')}
                >
                  <option value="sm">{t('index.thumbSize.sm')}</option>
                  <option value="md">{t('index.thumbSize.md')}</option>
                  <option value="lg">{t('index.thumbSize.lg')}</option>
                </Select>
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.defaultThumbnailSizeHelp')}</div>
              </div>

              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.defaultSortFieldLabel')}</div>
                <Select
                  size="sm"
                  value={v.defaultSortField}
                  onChange={(e) => set('defaultSortField', e.target.value as 'title' | 'catalog_id' | 'wsi_filename')}
                >
                  <option value="catalog_id">{t('index.sort.catalog_id')}</option>
                  <option value="title">{t('index.sort.title')}</option>
                  <option value="wsi_filename">{t('index.sort.wsi_filename')}</option>
                </Select>
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.defaultSortFieldHelp')}</div>
              </div>

              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.filterSortOrderLabel')}</div>
                <Select
                  size="sm"
                  value={v.filterSortOrder}
                  onChange={(e) => set('filterSortOrder', e.target.value as 'count' | 'alpha')}
                >
                  <option value="count">{t('adminDash.filterSortOrderCount')}</option>
                  <option value="alpha">{t('adminDash.filterSortOrderAlpha')}</option>
                </Select>
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.filterSortOrderHelp')}</div>
              </div>

              <div style={itemRow}>
                <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={v.showFilterButton}
                    onChange={(e) => set('showFilterButton', e.target.checked)}
                  />
                  {t('adminDash.showFilterButton')}
                </label>
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.showFilterButtonHelp')}</div>
              </div>

              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.maxIndexItemsLabel')}</div>
                <input
                  type="number"
                  min={MAX_INDEX_ITEMS_MIN}
                  max={MAX_INDEX_ITEMS_MAX}
                  step={100}
                  value={maxIndexItemsRaw}
                  onChange={(e) => setMaxIndexItemsRaw(e.target.value)}
                  onBlur={commitMaxIndexItems}
                  style={{ width: 100, boxSizing: 'border-box' }}
                />
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.maxIndexItemsHelp')}</div>
              </div>

              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.indexToursPerSlideLabel')}</div>
                <input
                  type="number"
                  min={INDEX_TOURS_PER_SLIDE_MIN}
                  max={INDEX_TOURS_PER_SLIDE_MAX}
                  step={1}
                  value={indexToursPerSlideRaw}
                  onChange={(e) => setIndexToursPerSlideRaw(e.target.value)}
                  onBlur={commitIndexToursPerSlide}
                  style={{ width: 100, boxSizing: 'border-box' }}
                />
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.indexToursPerSlideHelp')}</div>
              </div>

              <div style={itemRow}>
                <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={v.showAdminLink}
                    onChange={(e) => set('showAdminLink', e.target.checked)}
                  />
                  {t('adminDash.showAdminLink')}
                </label>
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.showAdminLinkHelp')}</div>
              </div>
            </div>
          </div>

          {/* Always-on header background + title color.
              Sits above the per-slot seasonal section because it's the
              lower-priority "default" that the seasonal slots override. */}
          <div style={{ borderLeft: '3px solid var(--color-border-strong)', paddingLeft: 8, marginTop: 4 }}>
            <div style={subHeading}>{t('adminDash.headerDefault.sectionTitle')}</div>
            <LockFieldset locked={headerResetting} style={{ display: 'grid', gap: 6 }}>
              <LockFieldset locked={headerImageRequest.busy} style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.headerDefault.imageLabel')}</div>
                {v.headerDefaultImageUrl && (
                  <>
                    <div
                      aria-label={t('adminDash.headerDefault.previewAlt')}
                      // 52px matches the actual painted area on the top page:
                      // Row 1 of IndexHeader is align-items:center, and the
                      // LanguageSwitcher's Select.sm (~32px) is the tallest
                      // child, so total = 32 + 10 + 10 padding = 52px.
                      style={{
                        marginBottom: 6,
                        height: 52,
                        border: '1px solid var(--color-border)',
                        borderRadius: 4,
                        backgroundImage: cssImageUrl(headerPreview),
                        backgroundRepeat: 'repeat',
                      }}
                    />
                    {/* Image-only delete, parallel to the favicon's
                        "Revert to default" button. The section's Reset
                        button below clears both image AND title color;
                        this button leaves the title color alone. */}
                    <div style={{ marginBottom: 6 }}>
                      <button type="button" onClick={onDeleteHeaderDefaultBackground} style={{ fontSize: 11 }}>
                        {t('adminDash.headerDefault.removeImage')}
                      </button>
                    </div>
                  </>
                )}
                <input
                  type="file"
                  accept=".png,.apng,.gif,image/png,image/gif"
                  onChange={onUploadHeaderDefaultBackground}
                  style={{ fontSize: 12, boxSizing: 'border-box', width: '100%' }}
                />
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.headerDefault.imageHelp')}</div>
              </LockFieldset>
              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.headerDefault.titleColorLabel')}</div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                  <input
                    type="color"
                    // The native color picker only accepts a full "#rrggbb"
                    // value; if the text input currently holds something
                    // partial like "#1a", fall back to black so the picker
                    // never silently snaps to a stale color.
                    value={isValidHexColor(v.headerDefaultTitleColor) ? v.headerDefaultTitleColor.toLowerCase() : '#000000'}
                    // The HTML spec guarantees this input emits a lowercase
                    // "#rrggbb" string, so no manual normalization needed.
                    onChange={(e) => set('headerDefaultTitleColor', e.target.value)}
                    style={{ width: 40, height: 28, padding: 0, border: '1px solid var(--color-border)', borderRadius: 4 }}
                    aria-label={t('adminDash.headerDefault.titleColorLabel')}
                  />
                  <input
                    type="text"
                    value={v.headerDefaultTitleColor}
                    placeholder="#1a2b3c"
                    // Don't lowercase per keystroke — typing "F" only to see
                    // "f" appear is jarring (and interferes with IME / caret
                    // behaviour). The canonical lowercased form is produced
                    // on blur, mirroring the server's _normalize_title_color
                    // so the persisted shape stays consistent.
                    onChange={(e) => set('headerDefaultTitleColor', e.target.value)}
                    onBlur={(e) => set('headerDefaultTitleColor', e.target.value.trim().toLowerCase())}
                    style={{ width: 100, boxSizing: 'border-box', fontFamily: 'monospace', fontSize: 12 }}
                  />
                  {v.headerDefaultTitleColor && (
                    <button
                      type="button"
                      onClick={() => set('headerDefaultTitleColor', '')}
                      style={{ fontSize: 11 }}
                    >
                      {t('adminDash.headerDefault.clearColor')}
                    </button>
                  )}
                </div>
                {headerColorInvalid && (
                  <div style={{ fontSize: 11, color: 'var(--color-danger-fg)', marginTop: 3 }}>
                    {t('adminDash.invalidColorHex')}
                  </div>
                )}
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.headerDefault.titleColorHelp')}</div>
              </div>
              <div style={{ paddingTop: 2 }}>
                <button type="button" onClick={onResetHeaderDefault} disabled={request.key !== null || headerImageRequest.busy} style={{ fontSize: 11 }}>
                  {t('adminDash.headerDefault.resetButton')}
                </button>
              </div>
            </LockFieldset>
          </div>
        </LockFieldset>

        <AdminSeasonalBackgroundsSection />

        <LockFieldset locked={notLoaded} style={{ display: 'grid', gap: 10 }}>
          {/* viewer — favicon + max digital zoom */}
          <div style={{ borderLeft: '3px solid var(--color-border-strong)', paddingLeft: 8, marginTop: 4 }}>
            <div style={subHeading}>{t('adminDash.sectionViewer')}</div>
            <div style={{ display: 'grid', gap: 6 }}>
              <LockFieldset locked={faviconRequest.busy} style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.faviconImage')}</div>
                {v.faviconUrl && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'center', gap: 8 }}>
                    <img src={faviconPreview || undefined} style={{ width: 32, height: 32, objectFit: 'contain', border: '1px solid var(--color-border)', borderRadius: 4 }} alt={t('adminDash.faviconPreviewAlt')} />
                    <button type="button" onClick={onDeleteFavicon} style={{ fontSize: 11 }}>{t('adminDash.faviconRevertToDefault')}</button>
                  </div>
                )}
                <input type="file" accept=".ico,.png" onChange={onUploadFavicon} style={{ fontSize: 12, boxSizing: 'border-box', width: '100%' }} />
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.faviconHelp')}</div>
              </LockFieldset>

              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.maxZoomPixelRatioLabel')}</div>
                <input
                  type="number"
                  min={1.0}
                  max={2.0}
                  step={0.1}
                  value={maxZoomRaw}
                  onChange={(e) => setMaxZoomRaw(e.target.value)}
                  onBlur={commitMaxZoom}
                  style={{ width: 80, boxSizing: 'border-box' }}
                />
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.maxZoomPixelRatioHelp')}</div>
              </div>

              <div style={{ ...itemRow, display: 'grid', gap: 0 }}>
                <div style={{ fontSize: 12, marginBottom: 6 }}>{t('adminDash.sectionViewerOverlay')}</div>
                {([
                  ['showZoomIndicator', t('adminDash.zoomIndicator')],
                  ['showScaleBar',      t('adminDash.scaleBar')],
                  ['showMetadataPanel', t('adminDash.metadataPanel')],
                ] as const).map(([key, label], i, arr) => (
                  <label key={label} style={{
                    fontSize: 12,
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    cursor: 'pointer',
                    padding: '6px 0',
                    borderBottom: i < arr.length - 1 ? '1px solid var(--color-border)' : 'none',
                  }}>
                    <input type="checkbox" checked={v[key]} onChange={(e) => set(key, e.target.checked)} />
                    {label}
                  </label>
                ))}
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 6 }}>
                  {t('adminDash.overlayHelp')}
                </div>
              </div>
            </div>
          </div>

          {/* info panel of the slide and tour viewers */}
          <div style={{ borderLeft: '3px solid var(--color-border-strong)', paddingLeft: 8, marginTop: 4 }}>
            <div style={subHeading}>{t('adminDash.sectionSlideViewer')}</div>
            <div style={{ display: 'grid', gap: 6 }}>
              <LockFieldset locked={logoRequest.busy} style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.logoImage')}</div>
                {v.logoUrl && (
                  <div style={{ marginBottom: 6, display: 'flex', alignItems: 'center', gap: 8 }}>
                    <img src={logoPreview || undefined} style={{ maxHeight: 40, maxWidth: 160, objectFit: 'contain', border: '1px solid var(--color-border)', borderRadius: 4 }} alt={t('adminDash.logoPreviewAlt')} />
                    <button type="button" onClick={onDeleteLogo} style={{ fontSize: 11 }}>{t('adminDash.remove')}</button>
                  </div>
                )}
                <input type="file" accept=".png,.jpg,.jpeg,.webp,.gif" onChange={onUploadLogo} style={{ fontSize: 12, boxSizing: 'border-box', width: '100%' }} />
              </LockFieldset>

              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.copyrightMsg')}</div>
                <input
                  value={v.copyrightMsg}
                  onChange={(e) => set('copyrightMsg', e.target.value)}
                  placeholder="© 2025 Your Organization"
                  // Mirrors the viewer_settings.copyright_msg column width.
                  maxLength={512}
                  style={{ width: '100%', boxSizing: 'border-box' }}
                />
              </div>

            </div>
          </div>

          {/* advanced viewer rendering tuning (OpenSeadragon internals).
              Isolated from the everyday display settings above because these
              affect tile-load timing / browser memory, apply to every viewer,
              and the defaults are right for almost all deployments. */}
          <div style={{ borderLeft: '3px solid var(--color-warning-fg, #92400e)', paddingLeft: 8, marginTop: 4 }}>
            <div style={subHeading}>{t('adminDash.sectionAdvancedRendering')}</div>
            <div
              style={{
                fontSize: 11,
                marginBottom: 6,
                padding: '6px 8px',
                borderRadius: 4,
                background: 'var(--color-warning-bg, #fef3c7)',
                color: 'var(--color-warning-fg, #92400e)',
              }}
            >
              {t('adminDash.advancedRenderingWarning')}
            </div>
            <div style={{ display: 'grid', gap: 6 }}>
              <div style={itemRow}>
                <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={v.immediateRender}
                    onChange={(e) => set('immediateRender', e.target.checked)}
                  />
                  {t('adminDash.immediateRenderLabel')}
                </label>
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.immediateRenderHelp')}</div>
              </div>

              <div style={itemRow}>
                <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.maxImageCacheCountLabel')}</div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                  <input
                    type="number"
                    min={MAX_IMAGE_CACHE_COUNT_MIN}
                    max={MAX_IMAGE_CACHE_COUNT_MAX}
                    step={50}
                    value={maxImageCacheCountRaw}
                    onChange={(e) => setMaxImageCacheCountRaw(e.target.value)}
                    onBlur={commitMaxImageCacheCount}
                    style={{ width: 100, boxSizing: 'border-box' }}
                  />
                  {v.maxImageCacheCount !== MAX_IMAGE_CACHE_COUNT_DEFAULT && (
                    <button type="button" onClick={resetMaxImageCacheCount} style={{ fontSize: 11 }}>
                      {t('adminDash.resetToDefault', { value: MAX_IMAGE_CACHE_COUNT_DEFAULT })}
                    </button>
                  )}
                </div>
                <div style={{ fontSize: 11, opacity: 0.6, marginTop: 3 }}>{t('adminDash.maxImageCacheCountHelp')}</div>
              </div>
            </div>
          </div>
        </LockFieldset>

        {/* sticky save button.
            The "unsaved changes" chip and the button accent make it
            obvious that the inline select / checkbox changes above are
            edits-in-flight and won't take effect until Save is pressed
            — without it, admins frequently expect changes to apply on
            select, since the controls give immediate visual feedback. */}
        <div style={{
          position: 'sticky',
          bottom: 0,
          marginLeft: -16,
          marginRight: -16,
          paddingLeft: 16,
          paddingRight: 16,
          paddingTop: 10,
          paddingBottom: 12,
          background: 'var(--color-surface)',
          borderTop: '1px solid var(--color-border)',
          zIndex: 1,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <button
              onClick={onSaveViewerSettings}
              disabled={request.key !== null || !isDirty || headerColorInvalid}
              style={isDirty && !headerColorInvalid ? {
                background: 'var(--color-brand-600)',
                color: 'var(--color-on-brand, #fff)',
                borderColor: 'var(--color-brand-600)',
              } : undefined}
            >
              {vsSaving ? t('adminDash.saving') : t('adminDash.saveViewerSettings')}
            </button>
            {isDirty && !vsSaving && (
              <span style={{
                fontSize: 11,
                fontWeight: 600,
                padding: '2px 8px',
                borderRadius: 4,
                background: 'var(--color-warning-bg, #fef3c7)',
                color: 'var(--color-warning-fg, #92400e)',
              }}>
                {t('adminDash.unsavedChanges')}
              </span>
            )}
          </div>
          <SectionMessage msg={vsMsg} onDismiss={clearVsMsg} style={{ marginTop: 6 }} />
        </div>
      </div>
    </>
  )
}
