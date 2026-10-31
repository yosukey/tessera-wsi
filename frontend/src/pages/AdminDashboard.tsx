import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router'
import AboutDialog from '../components/AboutDialog'
import { Badge, LinkButton } from '../components/ui'
import {
  Slide,
  TourSummary,
  MetadataOptions,
  AdminMe,
  Role,
  adminListSlides,
  adminCreateTour,
  adminDuplicateTour,
  adminDeleteSlide,
  adminBulkDeleteSlides,
  adminBulkPublishSlides,
  adminBulkSlideAttributes,
  adminBulkTourVisibility,
  adminBulkTourAttributes,
  adminBulkDeleteTours,
  type BulkTourResult,
  clearAdminSession,
  adminMe,
  adminListAllTours,
  adminGetViewerSettings,
  adminListMetadataOptions,
  getRoleFromToken,
  isAbortError,
  isAdminFromToken,
  isManagerOrAboveFromToken,
  isUnauthorizedError,
  staffGetViewerOverlayDefaults,
} from '../lib/api'
import { useI18n } from '../lib/i18n'
import { useMediaQuery } from '../hooks/useMediaQuery'
import { useInFlight } from '../hooks/useInFlight'
import { useStableCallback } from '../lib/useStableCallback'
import { logger } from '../lib/logger'
import { BASE_KEYS, META_DISPLAY_KEYS, displayFilename } from '../lib/admin/validation'
import { apiErrorMessage, failureMessage, withErrorDetail } from '../lib/admin/apiError'
import { canViewAnalytics, canViewServer } from '../lib/admin/permissions'
import { compareValues } from '../lib/naturalSort'
import { useSlideExport } from '../lib/admin/useSlideExport'
import { transferProgressText } from '../lib/admin/transferProgress'
import { useViewerSettingsForm, viewerSettingsFormFrom } from '../lib/admin/viewerSettingsForm'
import { STORAGE_KEYS } from '../lib/storageKeys'
import { safeSessionGetItem, safeSessionRemoveItem } from '../lib/safeStorage'
import type { SortField, SortOrder, ToursFilter } from '../lib/admin/validation'
import { loadSlideListState, saveSlideListState } from '../lib/admin/slideListState'
import AdminUploadSection from '../components/admin/AdminUploadSection'
import AdminMetadataSection from '../components/admin/AdminMetadataSection'
import AdminViewerSettingsSection from '../components/admin/AdminViewerSettingsSection'
import AdminNetworkAuthSection from '../components/admin/AdminNetworkAuthSection'
import AdminBackupSection from '../components/admin/AdminBackupSection'
import AdminImportSection from '../components/admin/AdminImportSection'
import AdminExportHistorySection from '../components/admin/AdminExportHistorySection'
import SlideEditModal from '../components/admin/SlideEditModal'
import SlideListToolbar from '../components/admin/SlideListToolbar'
import SlideFilterPanel from '../components/admin/SlideFilterPanel'
import SlideList from '../components/admin/SlideList'
import BulkActionBar from '../components/admin/BulkActionBar'
import BulkCategoryModal from '../components/admin/BulkCategoryModal'
import TourBulkActionBar from '../components/admin/TourBulkActionBar'
import type { Visibility } from '../lib/visibility'

// Tracks an element's rendered height via a callback ref + ResizeObserver so
// sticky siblings can be pinned directly beneath it. Returns 0 while the element
// is unmounted (e.g. a conditionally rendered panel that's currently collapsed),
// which conveniently zeroes out its contribution to dependent `top` offsets.
function useMeasuredHeight<T extends HTMLElement>() {
  const obs = useRef<ResizeObserver | null>(null)
  const [height, setHeight] = useState(0)
  const ref = useCallback((el: T | null) => {
    obs.current?.disconnect()
    if (!el) { setHeight(0); return }
    const measure = () => setHeight(el.getBoundingClientRect().height)
    measure()
    obs.current = new ResizeObserver(measure)
    obs.current.observe(el)
  }, [])
  return [ref, height] as const
}

// How long the slide search waits for typing to pause before filtering.
const SEARCH_DEBOUNCE_MS = 150

// Waits before each retry of a /me load that failed transiently (429 from the
// burst of mount-time requests, a 500, a network blip). The header's username
// and permission-gated links stay missing until one succeeds.
const ME_RETRY_DELAYS_MS = [1_000, 3_000, 10_000, 30_000]

type SidebarTab = 'upload' | 'metadata' | 'settings' | 'network' | 'backup' | 'import'

// Sidebar tabs per role. An unknown role (no usable token) gets only the tab
// every role has; the server enforces the real permissions either way.
function tabsForRole(role: Role | null): SidebarTab[] {
  if (role === 'admin') return ['upload', 'metadata', 'settings', 'network', 'backup', 'import']
  if (role === 'manager') return ['upload', 'metadata']
  return ['upload']
}

// `attrs` with one key's values added or removed, as the bulk metadata
// endpoint does it server-side (values trimmed, kept sorted, an emptied key
// dropped).
function withAttributeDelta(
  attrs: Record<string, string[]> | null | undefined,
  key: string,
  values: string[],
  mode: 'add' | 'remove',
): Record<string, string[]> {
  const current = new Set(attrs?.[key] || [])
  for (const v of values.map(v => v.trim()).filter(Boolean)) {
    if (mode === 'add') current.add(v); else current.delete(v)
  }
  const next = { ...(attrs || {}) }
  if (current.size > 0) next[key] = Array.from(current).sort()
  else delete next[key]
  return next
}

// The flat all-tours list keyed by slide id, the shape SlideList reads.
function groupToursBySlide(tours: TourSummary[]): Record<number, TourSummary[]> {
  const bySlide: Record<number, TourSummary[]> = {}
  for (const tour of tours) (bySlide[tour.slide_id] ||= []).push(tour)
  return bySlide
}

export default function AdminDashboard() {
  const { t } = useI18n()
  const nav = useNavigate()
  // The desktop layout's 460px sidebar + 600px min main pane need ~1060px
  // side by side, which overflows iPad widths (768/834 portrait, 1024
  // landscape). Below that, keep the side-by-side layout but drop the fixed
  // widths for a fluid 4:6 (sidebar:list) split so it fits the screen instead
  // of overflowing horizontally.
  const narrow = useMediaQuery('(max-width: 1080px)')

  // Sticky stacking for the slide-list controls: the header (title + toolbar)
  // pins at the top of the scrollable main pane, the filter panel pins directly
  // beneath it, and the bulk-action bar pins beneath whatever is above it. Each
  // sticky element's `top` offset is the summed height of the elements stacked
  // above it, measured live (ResizeObserver) so the offsets stay correct as the
  // toolbar wraps or the filter panel opens/closes.
  const [slideHeaderRef, slideHeaderHeight] = useMeasuredHeight<HTMLDivElement>()
  const [filterPanelRef, filterPanelHeight] = useMeasuredHeight<HTMLDivElement>()

  const [me, setMe] = useState<AdminMe | null>(null)
  const [slides, setSlides] = useState<Slide[]>([])
  const [slideTours, setSlideTours] = useState<Record<number, TourSummary[]>>({})
  const [edit, setEdit] = useState<Slide | null>(null)
  const [metaOptions, setMetaOptions] = useState<MetadataOptions>({})
  // Bumped after a slide save / bulk category assignment so the Metadata tab
  // (if mounted) re-fetches its usage counts without a page reload.
  const [metaRefreshSignal, setMetaRefreshSignal] = useState(0)
  const bumpMetaRefresh = useCallback(() => setMetaRefreshSignal((n) => n + 1), [])

  // The Settings tab's viewer-settings form, with its saved baseline for the
  // "Unsaved changes" indicator (see lib/admin/viewerSettingsForm).
  const viewerForm = useViewerSettingsForm()
  const loadViewerForm = viewerForm.load
  // The three metadata-display lists, part of the same settings but edited
  // and saved on the Metadata tab.
  const [vsExtraAttrs, setVsExtraAttrs] = useState<string[]>([])
  const [vsIndexCardAttrs, setVsIndexCardAttrs] = useState<string[]>([])
  const [vsIndexFilterAttrs, setVsIndexFilterAttrs] = useState<string[]>([])
  // The site title the server has persisted (initial fetch + successful
  // save). The browser tab title reads from this rather than the form so it
  // doesn't flicker on every keystroke while an admin types into the Site
  // Title field — unsaved edits shouldn't bleed into the tab.
  const [persistedSiteTitle, setPersistedSiteTitle] = useState('')
  // Same baseline snapshot mechanism as the form's, but scoped to the three
  // metadata-display lists owned by the Metadata tab. Lives here so a
  // sidebar tab switch doesn't unmount the section and drop the baseline
  // mid-edit.
  const [mdSavedSnapshot, setMdSavedSnapshot] = useState('')

  // Slides list: search, sort, filter. Restored from, and saved back to,
  // sessionStorage so leaving for another admin page (Server Info, a tour
  // editor, a viewer) and coming back keeps them (see lib/admin/slideListState).
  const [savedListState] = useState(loadSlideListState)
  const [searchQ, setSearchQ] = useState(savedListState.searchQ)
  const [sortField, setSortField] = useState<SortField>(savedListState.sortField)
  const [sortOrder, setSortOrder] = useState<SortOrder>(savedListState.sortOrder)
  const [attrFilters, setAttrFilters] = useState<Record<string, Set<string>>>(savedListState.attrFilters)
  const [vendorFilter, setVendorFilter] = useState<Set<string>>(savedListState.vendorFilter)
  const [toursFilter, setToursFilter] = useState<ToursFilter>(savedListState.toursFilter)
  const [filtersOpen, setFiltersOpen] = useState(savedListState.filtersOpen)
  useEffect(() => {
    saveSlideListState({ searchQ, sortField, sortOrder, attrFilters, vendorFilter, toursFilter, filtersOpen })
  }, [searchQ, sortField, sortOrder, attrFilters, vendorFilter, toursFilter, filtersOpen])

  // Bulk-selection state. Holds a raw set of slide ids; bulk actions operate
  // on `selectedVisible` (selection ∩ currently-visible) so a slide hidden by
  // a filter is never silently mutated.
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [bulkBusy, setBulkBusy] = useState(false)
  // A slide row's request (delete the slide, create a tour or a copy of
  // one): the list waits for it, so a second click can't delete twice or
  // create a second tour.
  const rowRequest = useInFlight()
  const [bulkCategoryOpen, setBulkCategoryOpen] = useState(false)
  // Tours are selected separately from slides (see TourBulkActionBar), by
  // tour id, and acted on the same way: only those on a visible slide.
  const [selectedTours, setSelectedTours] = useState<Set<number>>(new Set())
  const [tourCategoryOpen, setTourCategoryOpen] = useState(false)

  // Opens on the Backup & Restore tab when AdminBackupSection just reloaded the page
  // after a restore (it reads and removes the key once mounted).
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>(
    () => (safeSessionGetItem(STORAGE_KEYS.adminPostRestore) !== null ? 'backup' : 'upload'),
  )
  // True while AdminUploadSection has an upload batch in flight. The upload
  // queue is local state in that component, so switching tabs unmounts it and
  // silently drops the in-progress batch — lock the other tabs until it's done.
  const [uploadRunning, setUploadRunning] = useState(false)
  // Read when a request's answer would leave the page: an upload started
  // while it was out must not be dropped by the navigation.
  const uploadRunningRef = useRef(uploadRunning)
  uploadRunningRef.current = uploadRunning
  // True while AdminNetworkAuthSection has unsaved edits. Reported up (like
  // uploadRunning) so a sidebar tab switch away from the Network tab can
  // confirm before unmounting the section and dropping the edits.
  const [networkDirty, setNetworkDirty] = useState(false)
  // True while AdminImportSection is sending a bundle, which unmounting the
  // section cancels: leaving the Import / Export tab asks first, as above.
  const [importSending, setImportSending] = useState(false)
  const [showAbout, setShowAbout] = useState(false)
  // True once the metadata options fetch has actually returned. The
  // viewer-settings panel needs this to tell "no custom categories" apart
  // from "still loading" before deciding whether an extra_attr key is an
  // orphan (a category that was deleted while still referenced).
  const [metaOptionsLoaded, setMetaOptionsLoaded] = useState(false)
  // Surfaces the initial viewer-settings fetch failure to the Settings
  // tab. Save is already gated on a saved baseline existing (so the Save
  // button stays disabled and nothing can overwrite the server with
  // default values), but without a banner the admin sees fields filled
  // with defaults and no explanation of why their edits don't take.
  const [vsLoadError, setVsLoadError] = useState(false)
  // The overlay defaults the server has actually persisted (set on load and on
  // a successful viewer-settings save), for SlideEditModal's "inherited"
  // display. Kept apart from the form's three overlay fields, which are the
  // Settings tab's live checkboxes and may hold unsaved edits. null until loaded: before that there are only
  // guesses, and the modal must not decide "matches the global default →
  // store inherit" against a guess (with the real default off, ticking the
  // scale bar would save inherit and hide it).
  const [savedOverlays, setSavedOverlays] = useState<{
    showZoomIndicator: boolean
    showScaleBar: boolean
    showMetadataPanel: boolean
  } | null>(null)

  // Non-admin path: the full viewer settings are admin-only, but a manager's
  // slide editor still needs the three overlay defaults, which are public.
  async function loadOverlayDefaults(signal?: AbortSignal) {
    try {
      const d = await staffGetViewerOverlayDefaults({ signal })
      setSavedOverlays({
        showZoomIndicator: d.show_zoom_indicator,
        showScaleBar: d.show_scale_bar,
        showMetadataPanel: d.show_metadata_panel,
      })
    } catch (err) {
      if (isAbortError(err)) return
      logger.error('viewer overlay defaults load failed:', err)
    }
  }

  async function loadViewerSettings(signal?: AbortSignal) {
    if (!isAdminFromToken()) {
      // Only the slide editor (manager+) consumes anything from here.
      if (isManagerOrAboveFromToken()) await loadOverlayDefaults(signal)
      return
    }
    setVsLoadError(false)
    try {
      const vs = await adminGetViewerSettings({ signal })
      const form = viewerSettingsFormFrom(vs)
      const extraAttrs = vs.extra_attrs || []
      const indexCardAttrs = vs.index_card_attrs || []
      const indexFilterAttrs = vs.index_filter_attrs || []
      loadViewerForm(form)
      setVsExtraAttrs(extraAttrs)
      setVsIndexCardAttrs(indexCardAttrs)
      setVsIndexFilterAttrs(indexFilterAttrs)
      setPersistedSiteTitle(form.siteTitle)
      setSavedOverlays({
        showZoomIndicator: form.showZoomIndicator,
        showScaleBar: form.showScaleBar,
        showMetadataPanel: form.showMetadataPanel,
      })
      // Baseline for the Metadata tab's "Unsaved changes" indicator.
      setMdSavedSnapshot(JSON.stringify({
        extra: extraAttrs,
        card: indexCardAttrs,
        filter: indexFilterAttrs,
      }))
    } catch (err) {
      if (isAbortError(err)) return
      logger.error('viewer settings load failed:', err)
      // Surface the failure to the Settings tab. Save stays disabled
      // because the form's saved snapshot keeps its initial '' value, so even if
      // the admin edits fields they can't accidentally overwrite the
      // server with defaults.
      setVsLoadError(true)
    }
  }

  useEffect(() => {
    const ctrl = new AbortController()
    let meRetry: ReturnType<typeof setTimeout> | undefined
    const loadMe = (attempt: number) => {
      adminMe({ signal: ctrl.signal }).then(setMe).catch((err) => {
        if (isAbortError(err)) return
        // Only a real auth rejection sends the admin back to the login screen.
        // A transient failure (429 from the burst of mount-time requests, a
        // 500, a network blip) leaves them where they are — the token is still
        // valid and the other three loaders report their own errors in-place —
        // and is retried after a pause. The login screen replaces this entry:
        // Back from it must not return here only to be sent there again (a
        // signed-out admin pressing Back after Logout).
        if (isUnauthorizedError(err)) { nav('/admin', { replace: true }); return }
        logger.error('adminMe failed:', err)
        if (attempt < ME_RETRY_DELAYS_MS.length) {
          meRetry = setTimeout(() => loadMe(attempt + 1), ME_RETRY_DELAYS_MS[attempt])
        }
      })
    }
    loadMe(0)
    refreshSlides(ctrl.signal).catch((err) => { if (!isAbortError(err)) logger.error('refreshSlides failed:', err) })
    refreshMetadataOptions(ctrl.signal).catch((err) => { if (!isAbortError(err)) logger.error('refreshMetadataOptions failed:', err) })
    loadViewerSettings(ctrl.signal)
    return () => { ctrl.abort(); clearTimeout(meRetry) }
  }, [])

  // The browser tab title follows the persisted site title and the locale, so
  // it lives in its own effect keyed on both (`t` changes when the Japanese
  // bundle arrives or the admin switches language).
  useEffect(() => {
    document.title = t('adminDash.docTitle', { title: persistedSiteTitle })
  }, [persistedSiteTitle, t])

  // Key off the *set of slide IDs* rather than the `slides` array reference.
  // Without this, every per-slide field update (e.g. flipping is_published
  // through patchSlideLocal below) changes the slides array reference and
  // re-triggers a full `adminListAllTours()` fetch — that compounds with the
  // toggle's own POST and pushes the client into nginx's 120r/m burst=40
  // rate-limit window during normal interaction, surfacing as 429 errors and
  // toggles that "stick" or update in clumps.
  const slideIdsKey = useMemo(
    () => slides.map((s) => s.id).sort((a, b) => a - b).join(','),
    [slides],
  )
  // True while the tours list failed to load (or to refresh after a cascade):
  // the per-slide tours, the tours filter and the warning before unpublishing
  // a slide that has published tours are then working from missing or stale
  // data, so the list shows a notice with a retry.
  const [toursLoadFailed, setToursLoadFailed] = useState(false)
  // False until a tours list has first arrived: until then `slideTours` is an
  // empty placeholder, not "no tours", so the unpublish warning must not
  // count from it either.
  const [toursLoaded, setToursLoaded] = useState(false)
  const [toursReloadKey, setToursReloadKey] = useState(0)
  useEffect(() => {
    if (!slideIdsKey) {
      setSlideTours({})
      setToursLoadFailed(false)
      return
    }
    const ctrl = new AbortController()
    adminListAllTours({ signal: ctrl.signal }).then((tours) => {
      if (ctrl.signal.aborted) return
      setSlideTours(groupToursBySlide(tours))
      setToursLoadFailed(false)
      setToursLoaded(true)
    }).catch((err) => {
      if (isAbortError(err)) return
      logger.error('tours load failed:', err)
      setToursLoadFailed(true)
    })
    return () => ctrl.abort()
  }, [slideIdsKey, toursReloadKey])

  // Mutate a single slide locally so the publish-toggle path doesn't have
  // to re-fetch the entire slide list just to reflect one is_published flip.
  // A publish-state patch follows a server-side change, which also moves the
  // Metadata tab's published / draft usage counts.
  const patchSlideLocal = useCallback((slideId: number, patch: Partial<Slide>) => {
    setSlides((prev) => prev.map((s) => (s.id === slideId ? { ...s, ...patch } : s)))
    if (patch.is_published !== undefined) bumpMetaRefresh()
  }, [bumpMetaRefresh])
  // Same idea for tours: the caller passes the confirmed flags (and the tour
  // revision the toggles produced, which its next toggle must expect) —
  // avoiding an `adminListAllTours()` round-trip for the common case where
  // nothing else changed server-side.
  const patchTourLocal = useCallback((slideId: number, tourId: number, patch: Partial<TourSummary>) => {
    setSlideTours((prev) => {
      const tours = prev[slideId] || []
      return { ...prev, [slideId]: tours.map((t) => (t.id === tourId ? { ...t, ...patch } : t)) }
    })
  }, [])
  // Mirror a slide unpublish's server-side cascade: its published tours are
  // drafts now, each with the revision bump the cascade gave it (or this tab's
  // next toggle of one would be refused as a change made elsewhere). Applied
  // to the current tours, which a refetch while confirming may have replaced.
  const markToursDrafted = useCallback((slideId: number) => {
    setSlideTours((prev) => {
      const tours = prev[slideId]
      if (!tours) return prev
      return {
        ...prev,
        [slideId]: tours.map((tour) => (tour.is_published
          ? { ...tour, is_published: false, revision: typeof tour.revision === 'number' ? tour.revision + 1 : tour.revision }
          : tour)),
      }
    })
  }, [])

  async function refreshSlides(signal?: AbortSignal) {
    const s = await adminListSlides({ signal })
    if (signal?.aborted) return
    setSlides(s)
    // If the edit modal is open, point `edit` at the freshly fetched slide
    // object. Otherwise, after a save, closing and reopening the modal can
    // re-mount it with a slide reference whose values predate the save.
    // SlideEditModal's form-init effect depends on `slide.id` (not the
    // reference), so this update doesn't cause a destructive re-init when
    // the same slide stays open across the refresh.
    setEdit((prev) => {
      if (!prev) return prev
      const updated = s.find(x => x.id === prev.id)
      return updated ?? prev
    })
  }

  async function refreshMetadataOptions(signal?: AbortSignal) {
    const r = await adminListMetadataOptions({ signal })
    if (signal?.aborted) return
    setMetaOptions(r.options || {})
    setMetaOptionsLoaded(true)
  }

  // Re-fetch every slide's tours in one request and return them. The
  // per-slide-id effect only refetches when the *set* of slide ids changes, so
  // a bulk publish/unpublish (which cascades tours to draft without changing
  // the id set) needs this explicit refresh to reflect the cascade, and an
  // unpublish whose tours never loaded uses it to check them first. Rejects
  // (for the caller to report) after flagging the tours as not loaded.
  async function refreshAllTours(): Promise<Record<number, TourSummary[]>> {
    try {
      const bySlide = groupToursBySlide(await adminListAllTours())
      setSlideTours(bySlide)
      setToursLoadFailed(false)
      setToursLoaded(true)
      return bySlide
    } catch (err) {
      setToursLoadFailed(true)
      throw err
    }
  }

  const onDeleteSlide = useStableCallback(async (slideId: number, title: string) => {
    if (!confirm(t('adminDash.deleteSlideConfirm', { title }))) return
    await rowRequest.run(async () => {
      try {
        await adminDeleteSlide(slideId)
      } catch (err) {
        // The dashboard has no message area for actions fired from the slide
        // list, so the failure (and the server's reason for it) goes to alert().
        alert(failureMessage(t('adminDash.deleteSlideFailed'), err, t))
        return
      }
      // The slide's values no longer count towards the Metadata tab's usage.
      bumpMetaRefresh()
      // The DELETE has committed, so the refresh is best-effort: a transient
      // 429 / 5xx here must not be reported as a failed delete.
      try {
        await refreshSlides()
      } catch (err) {
        logger.error('slide list refresh after delete failed:', err)
      }
    })
  })

  // Opens a tour just created in its editor, unless an upload was started
  // while the request was out: leaving would cancel it, so the tour is listed
  // and the admin opens it once the upload is done.
  function openNewTour(id: number) {
    if (uploadRunningRef.current) {
      refreshAllTours().catch((err) => logger.error('tours refresh after create failed:', err))
      alert(t('adminDash.tourCreatedUploadRunning'))
      return
    }
    leaveTab(() => nav(`/admin/tours/${id}`))
  }

  // The new tour opens in its editor, leaving the dashboard the way a header
  // link does (see mayLeaveTab / leaveTab below). What leaving would drop is
  // confirmed before the title prompt, not once the tour exists.
  const onCreateTour = useStableCallback(async (slideId: number) => {
    if (!mayLeaveTab()) return
    const raw = prompt(t('adminDash.tourTitlePrompt'), t('adminDash.tourTitleDefault', { slideId, count: ((slideTours[slideId] || []).length + 1) }))
    if (raw === null) return
    const title = raw.trim()
    if (!title) return
    await rowRequest.run(async () => {
      try {
        const r = await adminCreateTour(slideId, title)
        openNewTour(r.id)
      } catch (err) {
        // Fired from a slide list row like delete above: alert() it.
        alert(failureMessage(t('adminDash.createTourFailed'), err, t))
      }
    })
  })

  // The copy opens in its editor, as a new tour does.
  const onDuplicateTour = useStableCallback(async (tour: TourSummary) => {
    if (!mayLeaveTab()) return
    // Room for the suffix within the 255-character title limit.
    const raw = prompt(t('adminDash.duplicateTourPrompt'), t('adminDash.duplicateTourTitleDefault', { title: tour.title.slice(0, 240) }))
    if (raw === null) return
    const title = raw.trim()
    if (!title) return
    await rowRequest.run(async () => {
      try {
        const r = await adminDuplicateTour(tour.id, title)
        openNewTour(r.id)
      } catch (err) {
        alert(failureMessage(t('adminDash.duplicateTourFailed'), err, t))
      }
    })
  })

  // An import adds slides and can bring new metadata categories / values with
  // them. The import itself has committed, so the refreshes are best-effort.
  const onImported = async () => {
    const results = await Promise.allSettled([refreshSlides(), refreshMetadataOptions()])
    for (const r of results) {
      if (r.status === 'rejected') logger.error('refresh after import failed:', r.reason)
    }
    bumpMetaRefresh()
  }

  const customMetaKeys = useMemo(
    () => Object.keys(metaOptions).filter(k => !BASE_KEYS.has(k)).sort(),
    [metaOptions],
  )

  // Credit texts already in use, most used first, for the edit modal's
  // completions (slides from one source should carry identical credits).
  const creditSuggestions = useMemo(() => {
    const counts = new Map<string, number>()
    for (const s of slides) {
      const v = (s.credit_text || '').trim()
      if (v) counts.set(v, (counts.get(v) || 0) + 1)
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([v]) => v)
  }, [slides])

  // The values the slides carry, per filter category.
  const slideAttrValues = useMemo(() => {
    const out: Record<string, Set<string>> = {}
    for (const key of META_DISPLAY_KEYS) {
      const set = new Set<string>()
      for (const s of slides) {
        const vals = (s.attributes || {})[key] || []
        for (const v of vals) if (v) set.add(v)
      }
      out[key] = set
    }
    return out
  }, [slides])

  const slideVendors = useMemo(() => {
    const set = new Set<string>()
    for (const s of slides) {
      if (s.vendor) set.add(s.vendor)
    }
    return set
  }, [slides])

  // Filter chips: the slides' values plus any selected one no slide carries
  // any more (a filter restored from the session after the value was removed,
  // or dropped by an edit), so a filter never narrows the list without a chip
  // to turn it off. compareValues is what the public index header sorts its
  // chips by too: a bare sort() compares UTF-16 code points, so values the
  // index lists as 1, 2, 3, 11 would read as 1, 11, 12, 2 here.
  const attrFacets = useMemo(() => {
    const out: Record<string, string[]> = {}
    for (const key of META_DISPLAY_KEYS) {
      const set = new Set([...slideAttrValues[key], ...(attrFilters[key] || [])])
      if (set.size) out[key] = Array.from(set).sort(compareValues)
    }
    return out
  }, [slideAttrValues, attrFilters])

  const vendorFacet = useMemo(
    () => Array.from(new Set([...slideVendors, ...vendorFilter])).sort(compareValues),
    [slideVendors, vendorFilter],
  )

  // The search box sits in the toolbar with its own clear (×) button and is
  // not part of the filter panel UI, so it shouldn't bump the "active
  // filters" badge — otherwise the badge counts something the panel can't
  // display or clear consistently.
  const activeFilterCount = useMemo(() => {
    let n = 0
    for (const set of Object.values(attrFilters) as Set<string>[]) n += set.size
    n += vendorFilter.size
    if (toursFilter !== 'all') n += 1
    return n
  }, [attrFilters, vendorFilter, toursFilter])

  // Filtering thousands of slides (and re-rendering every row) on each
  // keystroke would make typing lag; the input stays on the urgent searchQ while the
  // list follows a debounced copy, so a burst of keystrokes filters once. (A
  // useDeferredValue would re-render the whole list twice per
  // key - urgent pass with the old list, then the deferred one - and was measured
  // ~4x slower on 3000 slides.)
  const [debouncedSearchQ, setDebouncedSearchQ] = useState(searchQ)
  useEffect(() => {
    const handle = setTimeout(() => setDebouncedSearchQ(searchQ), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(handle)
  }, [searchQ])
  const visibleSlides = useMemo(() => {
    const q = debouncedSearchQ.trim().toLowerCase()
    const filtered = slides.filter(s => {
      if (q) {
        // Search across the visible row identifiers — title, catalog id,
        // filename, the slide id badge (#42), and the attribute values
        // (stain / organ / tags / etc.) that are surfaced in the
        // metadata row. Without the latter two, admins can't search by
        // values they can see in the row, which is consistently
        // confusing.
        const attrValues: string[] = []
        for (const vs of Object.values(s.attributes || {})) {
          if (Array.isArray(vs)) for (const v of vs) if (v) attrValues.push(v)
        }
        const hay = [s.title, s.catalog_id, s.wsi_filename, String(s.id), ...attrValues]
          .filter(Boolean)
          .map((v: string) => v.toLowerCase())
          .join(' ')
        if (!hay.includes(q)) return false
      }
      for (const [key, picked] of Object.entries(attrFilters) as [string, Set<string>][]) {
        if (!picked.size) continue
        const vals = new Set<string>((s.attributes || {})[key] || [])
        let hit = false
        for (const v of picked) { if (vals.has(v)) { hit = true; break } }
        if (!hit) return false
      }
      if (vendorFilter.size && !vendorFilter.has(s.vendor || '')) return false
      if (toursFilter !== 'all') {
        const hasT = ((slideTours[s.id] || []).length) > 0
        if (toursFilter === 'has' && !hasT) return false
        if (toursFilter === 'none' && hasT) return false
      }
      return true
    })

    const dir = sortOrder === 'asc' ? 1 : -1
    const cmpStr = (a: string, b: string) => a.localeCompare(b)
    const sortValue = (s: any): number | string => {
      switch (sortField) {
        case 'id': return Number(s.id) || 0
        case 'title': return String(s.title || '')
        case 'catalog_id': return String(s.catalog_id || '')
        case 'wsi_filename': return displayFilename(String(s.wsi_filename || ''))
        case 'vendor': return String(s.vendor || '')
        case 'pixels': return (Number(s.width) || 0) * (Number(s.height) || 0)
        case 'mpp': return Number(s.mpp_x) || Number(s.mpp_y) || 0
        case 'tours': return (slideTours[s.id] || []).length
        case 'created_at': return String(s.created_at || '')
        default: return 0
      }
    }

    return [...filtered].sort((a, b) => {
      // Always push slides with missing MPP to the end when sorting by
      // MPP. Mirror the display-side definition of "has MPP" (mpp_x ||
      // mpp_y; see SlideList.tsx) so a slide that only carries mpp_y
      // is treated as present here too, instead of being demoted to
      // the tail.
      if (sortField === 'mpp') {
        const av = Number(a.mpp_x) || Number(a.mpp_y) || 0
        const bv = Number(b.mpp_x) || Number(b.mpp_y) || 0
        if (!av && !bv) return 0
        if (!av) return 1
        if (!bv) return -1
      }
      const av = sortValue(a)
      const bv = sortValue(b)
      if (typeof av === 'number' && typeof bv === 'number') return dir * (av - bv)
      return dir * cmpStr(String(av), String(bv))
    })
  }, [slides, slideTours, debouncedSearchQ, attrFilters, vendorFilter, toursFilter, sortField, sortOrder])

  const totalFileBytes = useMemo(
    () => slides.reduce((sum, s) => sum + (s.file_size_bytes || 0), 0),
    [slides],
  )

  const toggleAttrFilter = (key: string, value: string) => {
    setAttrFilters(prev => {
      const next = { ...prev }
      const set = new Set(next[key] || [])
      if (set.has(value)) set.delete(value); else set.add(value)
      if (set.size) next[key] = set
      else delete next[key]
      return next
    })
  }

  const toggleVendorFilter = (value: string) => {
    setVendorFilter(prev => {
      const next = new Set(prev)
      if (next.has(value)) next.delete(value); else next.add(value)
      return next
    })
  }

  // The role from /me, or from the login token until /me answers (or while it
  // keeps failing), so the tabs never show more than this role may use.
  const role: Role | null = me?.role ?? getRoleFromToken()
  // The per-role permission flags only come with /me; an admin's token role
  // is enough before then.
  const canSeeAnalytics = role === 'admin' || (!!me && canViewAnalytics(me))
  const canSeeServer = role === 'admin' || (!!me && canViewServer(me))

  const tabs = useMemo(() => tabsForRole(role), [role])
  // The tab actually shown: the chosen one while this role has it.
  const activeTab: SidebarTab = tabs.includes(sidebarTab) ? sidebarTab : 'upload'

  // If the active tab disappears for the current role, fall back to 'upload'.
  // Only once the role is known: with no usable token the session is about to
  // be sent to the login screen, and a post-restore reopen must survive that
  // for the admin who logs back in.
  useEffect(() => {
    if (role !== null && !tabs.includes(sidebarTab)) {
      setSidebarTab('upload')
      // A post-restore reopen this role can't honour must not linger.
      safeSessionRemoveItem(STORAGE_KEYS.adminPostRestore)
    }
  }, [role, tabs, sidebarTab])

  const tabLabel = (tab: SidebarTab): string => {
    switch (tab) {
      case 'upload': return role === 'instructor' ? t('adminDash.tabSlides') : t('adminDash.tabUpload')
      case 'metadata': return t('adminDash.tabMetadata')
      case 'settings': return t('adminDash.tabSettings')
      case 'network': return t('adminDash.tabNetwork')
      case 'backup': return t('adminDash.tabBackup')
      case 'import': return t('adminDash.tabTransfer')
    }
  }

  // Switching sidebar tabs, following a header link or a slide list's link to
  // a tour editor, creating a tour and logging out all unmount the active
  // section: AdminNetworkAuthSection silently drops its in-progress IP-rule /
  // auth / LDAP changes, AdminImportSection cancels a bundle upload whose body
  // is still being sent. Each exit asks first while something is at stake.
  // Returns whether to go ahead. (The sections guard reload / tab close and
  // browser Back themselves.)
  const mayLeaveTab = (): boolean => {
    if (activeTab === 'network' && networkDirty) return window.confirm(t('adminDash.unsavedChangesConfirm'))
    if (activeTab === 'import' && importSending) return window.confirm(t('adminImport.uploadLeaveConfirm'))
    return true
  }
  // mayLeaveTab for an exit that follows at once: a second click on the way
  // out (a double click) doesn't ask again.
  const confirmLeaveTab = (): boolean => {
    if (!mayLeaveTab()) return false
    setNetworkDirty(false)
    setImportSending(false)
    return true
  }

  // Those exits, once confirmed (or with nothing at stake), run through the
  // navigation guard of the active section while it has one: the guard steps
  // back off the history entry it pushed (for unsaved edits, an upload run or
  // a bundle being sent, and still in place after a save or once the upload
  // is done) first, which would otherwise stay behind and cost Back an extra
  // press from the next page. Only one tab's section is mounted at a time.
  const tabLeaveRef = useRef<((go: () => void) => void) | null>(null)
  const leaveTab = (go: () => void) => {
    const leave = tabLeaveRef.current
    if (leave) leave(go)
    else go()
  }

  const requestTabSwitch = (tab: SidebarTab) => {
    if (tab === activeTab) return
    if (!confirmLeaveTab()) return
    leaveTab(() => setSidebarTab(tab))
  }

  // A click on an in-app link to another admin page. A modified click (new
  // tab / window) is left to the browser and keeps this page, and its edits,
  // in place.
  const onLeaveLinkClick = (e: React.MouseEvent<HTMLAnchorElement>, to: string) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    e.preventDefault()
    if (!confirmLeaveTab()) return
    leaveTab(() => nav(to))
  }
  // Props for the header's in-app links.
  const headerLink = (to: string) => ({
    to,
    onClick: (e: React.MouseEvent<HTMLAnchorElement>) => onLeaveLinkClick(e, to),
  })
  // The slide list's links to tour editors; stable for the memoised list.
  const onListLinkClick = useStableCallback(onLeaveLinkClick)

  // Asks whether to log out first, then (with something at stake) whether to
  // drop it.
  const onLogout = () => {
    if (!window.confirm(t('admin.logoutConfirm'))) return
    if (!confirmLeaveTab()) return
    clearAdminSession()
    leaveTab(() => nav('/admin'))
  }

  const clearAllSlideFilters = () => {
    // Leave searchQ alone: it has its own clear (×) affordance in the
    // toolbar and is intentionally not part of the panel's filter set.
    setAttrFilters({})
    setVendorFilter(new Set())
    setToursFilter('all')
  }

  // Bulk selection. `selectedVisible` is the only set bulk actions act on, so
  // slides hidden by the active filter are never touched.
  const selectedVisible = useMemo(
    () => visibleSlides.filter(s => selected.has(s.id)),
    [visibleSlides, selected],
  )
  const selectedVisibleIds = useMemo(() => selectedVisible.map(s => s.id), [selectedVisible])
  const allVisibleSelected = visibleSlides.length > 0 && selectedVisible.length === visibleSlides.length

  // Bulk export (admin-only). Reuses the shared export driver; a failure is
  // surfaced inline above the bulk-action bar.
  const bulkExport = useSlideExport()
  const [bulkExportErr, setBulkExportErr] = useState('')
  // Bumped after a successful export so the Import/Export tab's history list
  // refetches and shows the freshly-generated bundle.
  const [exportHistorySignal, setExportHistorySignal] = useState(0)
  async function onBulkExport() {
    setBulkExportErr('')
    try {
      await bulkExport.exportSlides(selectedVisibleIds)
      setExportHistorySignal((n) => n + 1)
    } catch (err) {
      setBulkExportErr(apiErrorMessage(err, t))
    }
  }

  // The tours on the visible slides, and the selected ones among them: the
  // tour bulk actions act on these, never on a tour whose slide is filtered
  // out.
  const visibleTours = useMemo(
    () => visibleSlides.flatMap(s => slideTours[s.id] || []),
    [visibleSlides, slideTours],
  )
  const selectedVisibleTours = useMemo(
    () => visibleTours.filter(tour => selectedTours.has(tour.id)),
    [visibleTours, selectedTours],
  )
  const allVisibleToursSelected = visibleTours.length > 0 && selectedVisibleTours.length === visibleTours.length

  const toggleSelectTour = useCallback((tourId: number) => {
    setSelectedTours(prev => {
      const next = new Set(prev)
      if (next.has(tourId)) next.delete(tourId); else next.add(tourId)
      return next
    })
  }, [])
  const selectAllVisibleTours = () => setSelectedTours(new Set(visibleTours.map(tour => tour.id)))
  const clearTourSelection = () => setSelectedTours(new Set())

  const toggleSelect = useCallback((slideId: number) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(slideId)) next.delete(slideId); else next.add(slideId)
      return next
    })
  }, [])
  const selectAllVisible = () => setSelected(new Set(visibleSlides.map(s => s.id)))
  const clearSelection = () => setSelected(new Set())

  // The bulk actions below have already COMMITTED on the server by the time they
  // refresh the list; a throttle or hiccup on that follow-up GET must not be
  // reported as a failed delete / publish / category change - but it must not be
  // silent either (the list would just keep showing the old state). Returns
  // whether the refresh worked; on failure the caller patches what it knows
  // locally and tells the admin the list is stale.
  async function refreshAfterCommit(refresh: () => Promise<unknown>, what: string): Promise<boolean> {
    try {
      await refresh()
      return true
    } catch (err) {
      logger.error(`${what}: list refresh after a committed bulk action failed:`, err)
      return false
    }
  }

  // Bulk-action failures go to alert() for the same reason onDeleteSlide's
  // do: the bulk bar in <main> has no message area of its own.
  async function onBulkDelete() {
    const ids = selectedVisibleIds
    if (ids.length === 0) return
    if (!confirm(t('adminDash.bulkDeleteConfirm', { count: ids.length }))) return
    setBulkBusy(true)
    try {
      const r = await adminBulkDeleteSlides(ids)
      bumpMetaRefresh()
      const refreshed = await refreshAfterCommit(() => refreshSlides(), 'bulk delete')
      if (!refreshed) {
        // Drop what the server confirmed gone so those rows can't be acted on.
        const gone = new Set([...r.deleted, ...r.not_found])
        setSlides((prev) => prev.filter((sl) => !gone.has(sl.id)))
        alert(t('adminDash.listRefreshFailed'))
      }
      if (r.failed.length > 0) {
        // Keep only the failed (retryable) ids selected; drop deleted + gone.
        setSelected(new Set(r.failed.map(f => f.slide_id)))
        alert(withErrorDetail(t('adminDash.bulkDeleteFailed', { count: r.failed.length }), r.failed[0]?.error))
      } else {
        clearSelection()
      }
    } catch (err) {
      alert(failureMessage(t('adminDash.bulkDeleteFailed', { count: ids.length }), err, t))
    } finally {
      setBulkBusy(false)
    }
  }

  // Unpublishing cascades each slide's published tours to draft server-side,
  // so both the bulk and the per-row unpublish confirm first. This counts the
  // published tours on `ids`. With the tours list not loaded (not answered
  // yet, or failed) the count is unknown, so it is fetched now rather than
  // the question skipped; null when that fails as well (the caller then asks
  // without a count).
  async function publishedToursOn(ids: number[]): Promise<number | null> {
    let tours: Record<number, TourSummary[]> | null = slideTours
    if (toursLoadFailed || !toursLoaded) {
      tours = await refreshAllTours().catch((err) => {
        logger.error('tours check before unpublish failed:', err)
        return null
      })
    }
    if (tours === null) return null
    const bySlide = tours
    return ids.reduce((n, id) => n + (bySlide[id] || []).filter((tour) => tour.is_published).length, 0)
  }

  // Whether to go ahead with a bulk unpublish.
  async function confirmUnpublishCascade(ids: number[]): Promise<boolean> {
    const publishedTours = await publishedToursOn(ids)
    if (publishedTours === null) return confirm(t('adminDash.bulkUnpublishConfirmUnknownTours', { count: ids.length }))
    return publishedTours === 0 || confirm(t('adminDash.bulkUnpublishConfirm', { count: ids.length, tours: publishedTours }))
  }

  // Whether to go ahead with returning one slide to draft from its row.
  const confirmSlideUnpublish = useStableCallback(async (slideId: number): Promise<boolean> => {
    const publishedTours = await publishedToursOn([slideId])
    if (publishedTours === null) return confirm(t('adminDash.slideUnpublishConfirmUnknownTours'))
    return publishedTours === 0 || confirm(t('adminDash.slideUnpublishConfirm', { count: publishedTours }))
  })

  async function onBulkPublish(publish: boolean) {
    const ids = selectedVisibleIds
    if (ids.length === 0) return
    setBulkBusy(true)
    try {
      if (!publish && !(await confirmUnpublishCascade(ids))) return
      const r = await adminBulkPublishSlides(ids, publish)
      // The Metadata tab's usage counts split published / draft.
      bumpMetaRefresh()
      const slidesRefreshed = await refreshAfterCommit(() => refreshSlides(), 'bulk publish')
      if (!slidesRefreshed) {
        for (const x of r.results) if (x.ok) patchSlideLocal(x.slide_id, { is_published: x.is_published })
      }
      // Unpublish cascades tours to draft server-side; the id-set effect won't
      // refetch them, so refresh explicitly.
      const toursRefreshed = await refreshAfterCommit(() => refreshAllTours(), 'bulk publish (tours)')
      if (!slidesRefreshed || !toursRefreshed) alert(t('adminDash.listRefreshFailed'))
      const skipped = r.results.filter(x => x.error === 'missing_fields').length
      const missingWsi = r.results.filter(x => x.error === 'missing_wsi').length
      const otherFailed = r.results.filter(x => !x.ok && x.error !== 'missing_fields' && x.error !== 'missing_wsi').length
      const notes: string[] = []
      if (skipped > 0) notes.push(t('adminDash.bulkPublishSkipped', { count: skipped }))
      if (missingWsi > 0) notes.push(t('adminDash.bulkPublishSkippedMissingWsi', { count: missingWsi }))
      if (otherFailed > 0) notes.push(t('adminDash.bulkPublishPartialFailed', { count: otherFailed }))
      if (notes.length > 0) alert(notes.join('\n'))
      clearSelection()
    } catch (err) {
      alert(failureMessage(t('adminDash.bulkPublishFailed'), err, t))
    } finally {
      setBulkBusy(false)
    }
  }

  async function onBulkCategory(key: string, values: string[], mode: 'add' | 'remove') {
    const ids = selectedVisibleIds
    if (ids.length === 0 || values.length === 0) return
    setBulkBusy(true)
    try {
      await adminBulkSlideAttributes(ids, key, values, mode)
      if (!(await refreshAfterCommit(() => refreshSlides(), 'bulk category'))) {
        alert(t('adminDash.listRefreshFailed'))
      }
      bumpMetaRefresh()
      setBulkCategoryOpen(false)
      clearSelection()
    } catch (err) {
      alert(failureMessage(t('adminDash.bulkCategoryFailed'), err, t))
    } finally {
      setBulkBusy(false)
    }
  }

  // A tour bulk action's tours leave the selection, except `keep` (the ones
  // a retry can fix). A selected tour the action didn't cover (its slide is
  // filtered out right now) stays selected, and one unticked while the
  // request was in flight stays unticked.
  function deselectToursAfterBulk(sent: number[], keep: number[]) {
    const kept = new Set(keep)
    setSelectedTours(prev => {
      const next = new Set(prev)
      for (const id of sent) if (!kept.has(id)) next.delete(id)
      return next
    })
  }

  // After a tour bulk action: reload the tours (the results carry revisions,
  // and a conflict means the listed copy is stale), keep the tours that
  // failed for a reason a retry can fix selected, and report what was
  // skipped. `patchLocal` applies what the server confirmed when the reload
  // fails, so the rows don't keep showing the old state.
  async function finishTourBulk(
    results: BulkTourResult[],
    what: string,
    patchLocal: (r: BulkTourResult, tour: TourSummary) => Partial<TourSummary>,
  ) {
    bumpMetaRefresh()
    if (!(await refreshAfterCommit(() => refreshAllTours(), what))) {
      const sent = new Map(selectedVisibleTours.map(tour => [tour.id, tour]))
      for (const r of results) {
        const tour = sent.get(r.tour_id)
        if (r.ok && r.changed && tour) patchTourLocal(tour.slide_id, tour.id, patchLocal(r, tour))
      }
      alert(t('adminDash.listRefreshFailed'))
    }
    const count = (error: string) => results.filter(r => r.error === error).length
    const parentDraft = count('parent_draft')
    const conflict = count('conflict')
    const notRegistered = count('not_registered')
    const other = results.filter(r => !r.ok).length - parentDraft - conflict - notRegistered
    const notes: string[] = []
    if (parentDraft > 0) notes.push(t('adminDash.bulkTourSkippedParentDraft', { count: parentDraft }))
    if (conflict > 0) notes.push(t('adminDash.bulkTourSkippedConflict', { count: conflict }))
    if (notRegistered > 0) notes.push(t('adminDash.bulkTourSkippedNotRegistered', { count: notRegistered }))
    if (other > 0) notes.push(t('adminDash.bulkTourPartialFailed', { count: other }))
    // Retrying helps a tour changed elsewhere (now reloaded), one whose
    // slide gets published first, or one a server error failed; not a gone
    // tour or a value no longer offered.
    const retryable = new Set(['conflict', 'parent_draft', 'failed'])
    deselectToursAfterBulk(
      selectedVisibleTours.map(tour => tour.id),
      results.filter(r => !r.ok && retryable.has(r.error ?? '')).map(r => r.tour_id),
    )
    if (notes.length > 0) alert(notes.join('\n'))
  }

  // A tour bulk request that failed as a whole (a network error, a gateway
  // timeout) may still have committed some tours server-side, each with its
  // own revision bump: reload, best-effort, so the rows and the Metadata
  // tab's counts don't stay stale and a retry doesn't meet this admin's own
  // changes as conflicts.
  async function resyncToursAfterFailedBulk(what: string) {
    bumpMetaRefresh()
    await refreshAfterCommit(() => refreshAllTours(), what)
  }

  async function onBulkTourVisibility(target: Visibility) {
    const tours = selectedVisibleTours
    if (tours.length === 0) return
    setBulkBusy(true)
    try {
      const r = await adminBulkTourVisibility(tours, target)
      await finishTourBulk(r.results, 'bulk tour visibility', (x) => ({
        is_published: x.is_published, unlisted: x.unlisted, revision: x.revision,
      }))
    } catch (err) {
      alert(failureMessage(t('adminDash.bulkTourVisibilityFailed'), err, t))
      await resyncToursAfterFailedBulk('bulk tour visibility (failed)')
    } finally {
      setBulkBusy(false)
    }
  }

  async function onBulkTourCategory(key: string, values: string[], mode: 'add' | 'remove') {
    const tours = selectedVisibleTours
    if (tours.length === 0 || values.length === 0) return
    setBulkBusy(true)
    try {
      const r = await adminBulkTourAttributes(tours, key, values, mode)
      setTourCategoryOpen(false)
      // Without the reload, apply the same change to the listed attributes
      // (and take the new revisions, or the next write would be refused).
      await finishTourBulk(r.results, 'bulk tour metadata', (x, tour) => ({
        revision: x.revision,
        attributes: withAttributeDelta(tour.attributes, key, values, mode),
      }))
    } catch (err) {
      alert(failureMessage(t('adminDash.bulkTourCategoryFailed'), err, t))
      await resyncToursAfterFailedBulk('bulk tour metadata (failed)')
    } finally {
      setBulkBusy(false)
    }
  }

  async function onBulkTourDelete() {
    const ids = selectedVisibleTours.map(tour => tour.id)
    if (ids.length === 0) return
    if (!confirm(t('adminDash.bulkTourDeleteConfirm', { count: ids.length }))) return
    setBulkBusy(true)
    try {
      const r = await adminBulkDeleteTours(ids)
      bumpMetaRefresh()
      if (!(await refreshAfterCommit(() => refreshAllTours(), 'bulk tour delete'))) {
        // Drop what the server confirmed gone so those rows can't be acted on.
        const gone = new Set([...r.deleted, ...r.not_found])
        setSlideTours((prev) => {
          const next: Record<number, TourSummary[]> = {}
          for (const [slideId, tours] of Object.entries(prev)) next[Number(slideId)] = tours.filter(tour => !gone.has(tour.id))
          return next
        })
        alert(t('adminDash.listRefreshFailed'))
      }
      // Keep only the failed (retryable) ids selected; drop deleted + gone.
      deselectToursAfterBulk(ids, r.failed.map(f => f.tour_id))
      if (r.failed.length > 0) {
        alert(t('adminDash.bulkTourDeleteFailed', { count: r.failed.length }))
      }
    } catch (err) {
      alert(failureMessage(t('adminDash.bulkTourDeleteFailed', { count: ids.length }), err, t))
      await resyncToursAfterFailedBulk('bulk tour delete (failed)')
    } finally {
      setBulkBusy(false)
    }
  }

  // While an upload batch is in flight, lock the whole dashboard except the
  // upload section. Switching tabs (handled below) or navigating off the page
  // (account / analytics / server / top page / logout) unmounts
  // AdminUploadSection and silently drops the in-flight queue; mutating the
  // slide list mid-upload is likewise unsafe. `inert` takes a subtree out of
  // pointer, keyboard, and a11y interaction in one shot — applied to the
  // left-pane action buttons and the entire <main> pane — while the dimming
  // makes the locked state visible. (`inert` is typed via src/react-inert.d.ts
  // since @types/react 18 predates it.)
  const lockInert = uploadRunning ? '' : undefined
  const lockedOpacity = uploadRunning ? 0.4 : undefined

  return (
    <div style={{ display: 'flex', height: '100vh', background: 'var(--color-bg)' }}>
      <aside style={{
        flex: narrow ? '4 1 0' : '0 0 460px',
        minWidth: 0,
        borderRight: '1px solid var(--color-border)',
        boxSizing: 'border-box',
        background: 'var(--color-surface)',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}>
        <div style={{ padding: '16px 16px 0', flexShrink: 0 }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>
            <div>
              <h2 style={{ margin: '0 0 4px' }}>{t('adminDash.title')}</h2>
              <div style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ opacity: 0.8 }}>{t('adminDash.loggedInAs', { username: me?.username || '' })}</span>
                {role && <RoleBadge role={role} />}
              </div>
              <div
                inert={lockInert}
                style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 8, rowGap: 8, alignItems: 'center', opacity: lockedOpacity }}
              >
                <LinkButton {...headerLink('/admin/account')} size="sm" variant="secondary">{t('adminDash.account')}</LinkButton>
                {canSeeAnalytics && (
                  <LinkButton {...headerLink('/admin/analytics')} size="sm" variant="secondary">{t('adminDash.tabAnalytics')}</LinkButton>
                )}
                {canSeeServer && (
                  <LinkButton {...headerLink('/admin/server')} size="sm" variant="secondary">{t('adminDash.tabServer')}</LinkButton>
                )}
                {canSeeServer && (
                  <LinkButton {...headerLink('/admin/performance')} size="sm" variant="secondary">{t('adminDash.tabPerformance')}</LinkButton>
                )}
                {role === 'admin' && (
                  <LinkButton {...headerLink('/admin/history')} size="sm" variant="secondary">{t('adminDash.tabHistory')}</LinkButton>
                )}
                {role === 'admin' && (
                  <LinkButton {...headerLink('/admin/logs')} size="sm" variant="secondary">{t('adminDash.tabLogs')}</LinkButton>
                )}
                <LinkButton
                  to="/"
                  size="sm"
                  variant="secondary"
                  target="_blank"
                  rel="noopener noreferrer"
                  rightIcon={
                    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden>
                      <path d="M6 3h7v7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                      <path d="M13 3 7 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                      <path d="M11 9.5V13H3V5h3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  }
                >
                  {t('adminDash.topPage')}
                </LinkButton>
                <button
                  type="button"
                  onClick={() => setShowAbout(true)}
                  // The About modal (and this trigger's tooltip / aria-label)
                  // is intentionally English-only — version, license, and
                  // attribution strings are not translated. Do not wire
                  // these through i18n.
                  title="About this app"
                  aria-label="About this app"
                  style={{
                    background: 'transparent',
                    border: 'none',
                    cursor: 'pointer',
                    color: 'var(--color-text-faint)',
                    padding: 0,
                    lineHeight: 1,
                    display: 'flex',
                    alignItems: 'center',
                  }}
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                    <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.4" />
                    <circle cx="8" cy="5.5" r="0.9" fill="currentColor" />
                    <path d="M8 7.5v3.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
                  </svg>
                </button>
              </div>
            </div>
            <button
              onClick={onLogout}
              disabled={uploadRunning}
              title={uploadRunning ? t('adminDash.lockedDuringUpload') : undefined}
              style={{ flexShrink: 0, marginTop: 2, opacity: lockedOpacity, cursor: uploadRunning ? 'not-allowed' : undefined }}
            >{t('admin.logout')}</button>
          </div>

          <div style={{ display: 'flex', gap: 0, marginTop: 16, borderBottom: '1px solid var(--color-border)' }}>
            {tabs.map(tab => {
              // While an upload is running, lock every tab except the active
              // one (always 'upload' in that state) so the operator can't
              // unmount the upload section and lose the in-flight queue.
              const locked = uploadRunning && tab !== activeTab
              return (
                <button
                  key={tab}
                  onClick={() => requestTabSwitch(tab)}
                  disabled={locked}
                  title={locked ? t('adminDash.tabLockedDuringUpload') : undefined}
                  style={{
                    padding: '6px 16px',
                    fontSize: 12,
                    fontWeight: activeTab === tab ? 700 : 400,
                    background: 'none',
                    border: 'none',
                    borderBottom: activeTab === tab ? '2px solid var(--color-brand-600)' : '2px solid transparent',
                    cursor: locked ? 'not-allowed' : 'pointer',
                    opacity: locked ? 0.35 : activeTab === tab ? 1 : 0.6,
                    marginBottom: -1,
                  }}
                >
                  {tabLabel(tab)}
                </button>
              )
            })}
          </div>
        </div>

        <div className="overscroll-panel" style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '0 16px 16px' }}>
          {activeTab === 'upload' && (
            <AdminUploadSection refreshSlides={refreshSlides} onRunningChange={setUploadRunning} leaveRef={tabLeaveRef} />
          )}
          {activeTab === 'metadata' && (
            <AdminMetadataSection
              metaOptions={metaOptions}
              setMetaOptions={setMetaOptions}
              customMetaKeys={customMetaKeys}
              refreshMetadataOptions={refreshMetadataOptions}
              refreshSlides={refreshSlides}
              refreshTours={refreshAllTours}
              vsExtraAttrs={vsExtraAttrs}
              setVsExtraAttrs={setVsExtraAttrs}
              vsIndexCardAttrs={vsIndexCardAttrs}
              setVsIndexCardAttrs={setVsIndexCardAttrs}
              vsIndexFilterAttrs={vsIndexFilterAttrs}
              setVsIndexFilterAttrs={setVsIndexFilterAttrs}
              mdSavedSnapshot={mdSavedSnapshot}
              setMdSavedSnapshot={setMdSavedSnapshot}
              metaOptionsLoaded={metaOptionsLoaded}
              countsRefreshSignal={metaRefreshSignal}
              displayLoadError={vsLoadError}
              onRetryDisplayLoad={() => { loadViewerSettings() }}
            />
          )}
          {activeTab === 'settings' && (
            <AdminViewerSettingsSection
              form={viewerForm}
              onSavedSiteTitle={setPersistedSiteTitle}
              onSavedOverlays={setSavedOverlays}
              loadError={vsLoadError}
              onRetryLoad={() => { loadViewerSettings() }}
            />
          )}
          {activeTab === 'network' && (
            <AdminNetworkAuthSection onDirtyChange={setNetworkDirty} leaveRef={tabLeaveRef} />
          )}
          {activeTab === 'backup' && (
            <AdminBackupSection />
          )}
          {activeTab === 'import' && (
            <div style={{ display: 'grid', gap: 24 }}>
              <AdminImportSection onImported={onImported} onSendingChange={setImportSending} leaveRef={tabLeaveRef} />
              <div style={{ borderTop: '1px solid var(--color-border)' }} />
              <AdminExportHistorySection reloadSignal={exportHistorySignal} />
            </div>
          )}
        </div>
      </aside>

      {/* On desktop `minWidth: 600` keeps the slide rows readable next to the
          fixed 460px sidebar (without it `flex: 1` would collapse them). When
          narrow the panes share the width as a fluid 4:6 split, so the floor
          is dropped to 0 to avoid forcing horizontal overflow. */}
      <main
        className="overscroll-panel"
        inert={lockInert}
        style={{ flex: narrow ? '6 1 0' : '1 1 0', minWidth: narrow ? 0 : 600, padding: '0 12px 12px', overflow: 'auto', opacity: lockedOpacity }}
      >
        {edit && isManagerOrAboveFromToken() && (
          <SlideEditModal
            slide={edit}
            metaOptions={metaOptions}
            customMetaKeys={customMetaKeys}
            globalOverlays={savedOverlays}
            creditSuggestions={creditSuggestions}
            onClose={() => setEdit(null)}
            onSaved={async () => {
              // A failed refresh is the modal's to report; the Metadata tab
              // still learns that the slide's values changed.
              try { await refreshSlides() } finally { bumpMetaRefresh() }
            }}
          />
        )}

        {/* Sticky header so the slide list title and sort/filter controls
            remain visible while scrolling through long slide lists. The main
            pane's top padding is dropped to 0 so `top: 0` pins this flush to
            the pane's visible top edge (sticky offsets are measured against
            the scroll container's padding box). This header supplies the top
            inset itself via its own padding. The negative side margins +
            matching horizontal padding extend the opaque background across the
            main pane's horizontal padding, so cards scrolling underneath
            aren't visible in the gutter. */}
        <div ref={slideHeaderRef} style={{
          position: 'sticky',
          top: 0,
          zIndex: 2,
          background: 'var(--color-bg)',
          marginLeft: -12,
          marginRight: -12,
          padding: '12px 12px 10px',
        }}>
          <h3 style={{ marginTop: 0 }}>{t('adminDash.slidesHeading')}</h3>

          <SlideListToolbar
            searchQ={searchQ}
            onSearchChange={setSearchQ}
            sortField={sortField}
            onSortFieldChange={setSortField}
            sortOrder={sortOrder}
            onSortOrderToggle={() => setSortOrder(o => o === 'asc' ? 'desc' : 'asc')}
            filtersOpen={filtersOpen}
            onFiltersToggle={() => setFiltersOpen(v => !v)}
            // The filter panel always exposes the Tours filter, which is
            // meaningful as soon as any slide exists — so the trigger should
            // appear whenever there are slides, not only when attr/vendor
            // facets are populated.
            hasFilterableData={slides.length > 0}
            activeFilterCount={activeFilterCount}
            visibleCount={visibleSlides.length}
            totalCount={slides.length}
            totalFileBytes={totalFileBytes}
          />
        </div>

        {/* Pin the filter panel directly below the sticky header so the active
            filter tags stay visible while scrolling. Same opaque background +
            gutter extension as the header; the wrapper owns the bottom gap so
            nothing peeks through a transparent margin while pinned. */}
        {filtersOpen && slides.length > 0 && (
          <div ref={filterPanelRef} style={{
            position: 'sticky',
            top: slideHeaderHeight,
            zIndex: 1,
            background: 'var(--color-bg)',
            marginLeft: -12,
            marginRight: -12,
            padding: '0 12px 10px',
          }}>
            <SlideFilterPanel
              attrFacets={attrFacets}
              vendorFacet={vendorFacet}
              attrFilters={attrFilters}
              vendorFilter={vendorFilter}
              toursFilter={toursFilter}
              activeFilterCount={activeFilterCount}
              onAttrFilterToggle={toggleAttrFilter}
              onVendorFilterToggle={toggleVendorFilter}
              onToursFilterChange={setToursFilter}
              onClearAll={clearAllSlideFilters}
            />
          </div>
        )}

        {/* Pin the bulk-action bar just below the header (and the filter panel
            when it's open) so the actions for the current selection stay
            reachable while scrolling a long list. The opaque background + gutter
            extension (matching the header) keep cards from showing through as
            they scroll underneath; the wrapper owns the bottom gap so nothing
            peeks through a transparent margin while pinned. */}
        {(selectedVisible.length > 0 || selectedVisibleTours.length > 0) && (
          <div style={{
            position: 'sticky',
            top: slideHeaderHeight + filterPanelHeight,
            zIndex: 1,
            background: 'var(--color-bg)',
            marginLeft: -12,
            marginRight: -12,
            padding: '0 12px 10px',
          }}>
            {/* The slides' export error belongs with the slide bar; with only
                tours selected it would be a stale message above theirs. */}
            {bulkExportErr && selectedVisible.length > 0 && (
              <div role="alert" style={{ marginBottom: 6, fontSize: 'var(--font-size-sm)', color: 'var(--color-danger-fg)' }}>
                {bulkExportErr}
              </div>
            )}
            {selectedVisible.length > 0 && (
            <BulkActionBar
              selectedCount={selectedVisible.length}
              totalVisible={visibleSlides.length}
              allVisibleSelected={allVisibleSelected}
              canManage={isManagerOrAboveFromToken()}
              canExport={isAdminFromToken()}
              busy={bulkBusy || rowRequest.busy}
              exportBusy={bulkExport.exporting}
              exportProgress={transferProgressText(t, bulkExport.progress)}
              exportPhase={bulkExport.progress?.phase ?? null}
              onSelectAll={selectAllVisible}
              onClear={clearSelection}
              onBulkPublish={onBulkPublish}
              onBulkDelete={onBulkDelete}
              onOpenCategory={() => setBulkCategoryOpen(true)}
              onExportSelected={onBulkExport}
            />
            )}
            {selectedVisibleTours.length > 0 && (
              <div style={{ marginTop: selectedVisible.length > 0 ? 6 : 0 }}>
                <TourBulkActionBar
                  selectedCount={selectedVisibleTours.length}
                  totalVisible={visibleTours.length}
                  allVisibleSelected={allVisibleToursSelected}
                  busy={bulkBusy || rowRequest.busy}
                  onSelectAll={selectAllVisibleTours}
                  onClear={clearTourSelection}
                  onVisibility={onBulkTourVisibility}
                  onOpenCategory={() => setTourCategoryOpen(true)}
                  onDelete={onBulkTourDelete}
                />
              </div>
            )}
          </div>
        )}

        {toursLoadFailed && slides.length > 0 && (
          <div role="alert" style={{ marginBottom: 8, fontSize: 'var(--font-size-sm)', color: 'var(--color-danger-fg)', display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span>{t('adminDash.toursLoadFailed')}</span>
            <button type="button" onClick={() => setToursReloadKey((k) => k + 1)} style={{ fontSize: 12 }}>
              {t('adminDash.retry')}
            </button>
          </div>
        )}

        {/* Locked while a bulk action is out: it acts on the selection at the
            click and then clears it, so a slide ticked meanwhile would be
            unticked unseen, and a row action would race its refresh. Also
            while a row's own request is out (see rowRequest). */}
        <div inert={bulkBusy || rowRequest.busy ? '' : undefined}>
          <SlideList
            slides={visibleSlides}
            slideTours={slideTours}
            selected={selected}
            onToggleSelect={toggleSelect}
            selectedTours={selectedTours}
            onToggleSelectTour={toggleSelectTour}
            onOpenEdit={setEdit}
            onDeleteSlide={onDeleteSlide}
            onCreateTour={onCreateTour}
            onDuplicateTour={onDuplicateTour}
            onLinkClick={onListLinkClick}
            onPatchSlideLocal={patchSlideLocal}
            onPatchTourLocal={patchTourLocal}
            onConfirmUnpublish={confirmSlideUnpublish}
            onToursDrafted={markToursDrafted}
          />
        </div>
      </main>

      {bulkCategoryOpen && isManagerOrAboveFromToken() && (
        <BulkCategoryModal
          target="slides"
          items={selectedVisible}
          metaOptions={metaOptions}
          customMetaKeys={customMetaKeys}
          onApply={onBulkCategory}
          onClose={() => setBulkCategoryOpen(false)}
          busy={bulkBusy}
        />
      )}

      {tourCategoryOpen && (
        <BulkCategoryModal
          target="tours"
          items={selectedVisibleTours}
          metaOptions={metaOptions}
          customMetaKeys={customMetaKeys}
          onApply={onBulkTourCategory}
          onClose={() => setTourCategoryOpen(false)}
          busy={bulkBusy}
        />
      )}

      {showAbout && <AboutDialog onClose={() => setShowAbout(false)} />}
    </div>
  )
}

function RoleBadge({ role }: { role: Role }) {
  const { t } = useI18n()
  const tone: 'brand' | 'success' | 'info' =
    role === 'admin' ? 'brand' : role === 'manager' ? 'success' : 'info'
  return <Badge tone={tone}>{t(`roles.${role}`)}</Badge>
}
