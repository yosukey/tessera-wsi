import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import type { Facets, IndexTypeFilter, SeasonalBackground, SlideListItem as SlideResult, TourIndexItem, TourSummary } from '../lib/api'
import { INDEX_TOURS_PER_SLIDE_DEFAULT, MAX_INDEX_ITEMS_DEFAULT, clampIndexToursPerSlide, clampMaxIndexItems, getFacets, getViewerSettings, isAbortError, listAllPublishedTours, listSlides, listTourIndex } from '../lib/api'
import { logger } from '../lib/logger'
import { pickActiveHeaderState } from '../lib/seasonalBackground'
import { useAuthGate } from '../lib/useAuthGate'
import { useI18n } from '../lib/i18n'
import LiveSessionBanner, { toBannerSessions, type LiveSessionForBanner } from '../components/LiveSessionBanner'
import { DiscoverySocket, type DiscoveryEvent, type DiscoverySessionEntry } from '../lib/discovery'
import { STORAGE_KEYS } from '../lib/storageKeys'
import { safeGetItem, safeSetItem } from '../lib/safeStorage'
import { BASE_KEYS, SORT_OPTIONS, TYPE_OPTIONS, THUMBNAIL_TILE_SIZES, THUMBNAIL_LIST_SIZES, THUMBNAIL_SIZE_OPTIONS } from '../components/index/types'
import type { SortField, Order, ViewMode, ThumbnailSize } from '../components/index/types'
import IndexHeader from '../components/index/IndexHeader'
import AboutDialog from '../components/AboutDialog'
import SlideCard from '../components/index/SlideCard'
import SlideListItem from '../components/index/SlideListItem'
import TourTile from '../components/index/TourTile'
import TourRow from '../components/index/TourRow'
import { facetsForView, visibleFilterKeys, groupToursBySlide, inlineTourCap, inlineTours, rememberSlideLabels, slideListParams, tourIndexParams, type SlideLabel } from '../components/index/results'

const VIEW_MODE_KEY = STORAGE_KEYS.viewMode
const THUMBNAIL_SIZE_KEY = STORAGE_KEYS.thumbnailSize

function EmptyState({ message }: { message: string }) {
  return (
    <div style={{
      textAlign: 'center', marginTop: 100,
      color: 'var(--color-text-subtle)',
      fontSize: 'var(--font-size-lg)',
    }}>
      {message}
    </div>
  )
}

export default function IndexPage() {
  useAuthGate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { t } = useI18n()
  const [facets, setFacets] = useState<Facets>({ groups: {} })
  // The active view's server result, rendered as-is: /api/slides for "All" /
  // "Slides", /api/index/tours for "Tours". Each holds one page capped at
  // maxIndexItems; its total is the size of the whole match set, so the
  // truncation banner compares the two.
  const [slides, setSlides] = useState<SlideResult[]>([])
  const [slidesTotal, setSlidesTotal] = useState(0)
  const [tourResults, setTourResults] = useState<TourIndexItem[]>([])
  const [tourResultsTotal, setTourResultsTotal] = useState(0)
  // True from a results request's start until it settles, so the empty state
  // ("No slides found." / "No tours found.") isn't flashed while the first
  // response for a view is still in flight.
  const [resultsPending, setResultsPending] = useState(true)
  // The view the results effect last fetched for (see that effect).
  const lastResultsViewRef = useRef<string | null>(null)
  // Every listed tour (/api/tours), for the slide cards' inline tour lists in
  // the "All" view only. Fetched once, the first time that view is shown.
  const [allTours, setAllTours] = useState<TourSummary[]>([])
  const [allToursLoaded, setAllToursLoaded] = useState(false)
  // Titles of every slide seen so far, for the live-session banner (see
  // rememberSlideLabels).
  const [slideLabels, setSlideLabels] = useState<Map<number, SlideLabel>>(() => new Map())
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [siteTitle, setSiteTitle] = useState('')
  const [seasonalBackgrounds, setSeasonalBackgrounds] = useState<SeasonalBackground[]>([])
  const [headerDefaultImageUrl, setHeaderDefaultImageUrl] = useState('')
  const [headerDefaultTitleColor, setHeaderDefaultTitleColor] = useState('')
  const [footerText, setFooterText] = useState('')
  const [defaultIndexType, setDefaultIndexType] = useState<IndexTypeFilter>('all')
  const [defaultSortField, setDefaultSortField] = useState<SortField>('catalog_id')
  // How the filter chips are ordered within each category: 'count' (most-used
  // values first, the default) or 'alpha' (alphabetical by value).
  const [filterSortOrder, setFilterSortOrder] = useState<'count' | 'alpha'>('count')
  const [showAdminLink, setShowAdminLink] = useState(false)
  // Per-instance cap. Owned by viewer_settings so an admin can dial it
  // down on a low-RAM device or up for a larger catalogue. Until the
  // settings fetch lands, the page renders against the same default the
  // backend bakes in, so the initial /api/slides request doesn't oscillate
  // between two per_page values.
  const [maxIndexItems, setMaxIndexItems] = useState<number>(MAX_INDEX_ITEMS_DEFAULT)
  // How many of a slide's tours are listed inline under it in the "all" view
  // before the rest collapse behind the per-slide expander. Owned by
  // viewer_settings so a deployment whose slides carry many tours each can dial
  // it down (0 = list none until asked) instead of having one slide fill the
  // viewport. Purely presentational — the tours are all already fetched, so
  // changing this never re-issues a request.
  const [toursPerSlide, setToursPerSlide] = useState<number>(INDEX_TOURS_PER_SLIDE_DEFAULT)
  // Whether getViewerSettings() has resolved (or failed) at least once. The
  // results effect below depends on defaultIndexType / defaultSortField /
  // maxIndexItems, which flip from the hardcoded defaults to the deployment's
  // real values when settings land. Gating the first results fetch on this flag
  // (mirrors ViewerPage) means a non-default deployment fetches once with the
  // final values — from the right endpoint — instead of once with the defaults
  // and again after settings resolve. Never reset to false — settings load
  // once.
  const [settingsLoaded, setSettingsLoaded] = useState(false)
  // Set when a catalog fetch (facets / tours / results) fails, so a 500/429
  // renders a visible banner rather than the "No slides yet" empty state, which
  // is otherwise indistinguishable from a genuinely empty catalog.
  // Split in two: the results effect re-runs (and clears its own flag) on every
  // search / filter / view change, which must not wipe a one-shot facets/tours
  // failure.
  const [catalogLoadError, setCatalogLoadError] = useState(false)
  const [resultsLoadError, setResultsLoadError] = useState(false)
  const loadError = catalogLoadError || resultsLoadError
  // Admin-controlled lists of metadata category keys for the slide cards
  // and the facet filter panel. Initial value is the hardcoded
  // default so the first paint (before /api/viewer/settings resolves) is
  // visually identical to the saved-default behaviour.
  const [cardAttrs, setCardAttrs] = useState<string[]>(['stain', 'organ', 'lab'])
  const [filterAttrs, setFilterAttrs] = useState<string[]>(['stain', 'organ', 'lab', 'tags'])
  // Validate the persisted values against the known enums before trusting
  // them: a stale / hand-edited localStorage entry ('xl', 'grid', …) would
  // otherwise flow straight through the cast. thumbnailSize in particular is
  // used as a key into THUMBNAIL_TILE_SIZES / THUMBNAIL_LIST_SIZES on the
  // render path, so an out-of-range value yields `undefined` and crashes the
  // page into the ErrorBoundary's fallback on every load. Fall back to the
  // default.
  const [viewMode, setViewMode] = useState<ViewMode>(() => {
    const raw = safeGetItem(VIEW_MODE_KEY)
    return raw === 'tile' || raw === 'list' ? raw : 'tile'
  })
  const [thumbnailSize, setThumbnailSize] = useState<ThumbnailSize>(() => {
    const raw = safeGetItem(THUMBNAIL_SIZE_KEY)
    return raw && (THUMBNAIL_SIZE_OPTIONS as string[]).includes(raw)
      ? (raw as ThumbnailSize)
      : 'lg'
  })

  const switchThumbnailSize = (size: ThumbnailSize) => {
    setThumbnailSize(size)
    safeSetItem(THUMBNAIL_SIZE_KEY, size)
  }

  const switchViewMode = (mode: ViewMode) => {
    setViewMode(mode)
    safeSetItem(VIEW_MODE_KEY, mode)
  }

  // Live-sync discovery: open WS only on this page (the banner surfaces active
  // live sessions; the connection is never carried into viewer / admin / tour pages).
  const [showAbout, setShowAbout] = useState(false)
  const [liveSessions, setLiveSessions] = useState<DiscoverySessionEntry[]>([])
  const discoveryRef = useRef<DiscoverySocket | null>(null)
  useEffect(() => {
    // The fields of an entry / event the banner uses (the labels name a slide
    // the loaded listing does not hold).
    const entry = (s: DiscoverySessionEntry): DiscoverySessionEntry => ({
      sid: s.sid,
      slide_id: s.slide_id,
      slide_public_id: s.slide_public_id,
      slide_title: s.slide_title ?? null,
      slide_catalog_id: s.slide_catalog_id ?? null,
    })
    discoveryRef.current = new DiscoverySocket({
      onEvent: (e: DiscoveryEvent) => {
        if (e.type === 'snapshot') {
          setLiveSessions(e.sessions.map(entry))
        } else if (e.type === 'started') {
          setLiveSessions((prev) =>
            prev.some((s) => s.sid === e.sid) ? prev : [...prev, entry(e)],
          )
        } else if (e.type === 'ended') {
          setLiveSessions((prev) => prev.filter((s) => s.sid !== e.sid))
        } else if (e.type === 'slide_changed') {
          // An unknown sid is added, not ignored: the snapshot hides a session
          // whose slide was unpublished or is link-only (and the server sends
          // ``ended`` when a presenter moves onto a link-only slide), and this
          // event goes out only for a published, listed slide, so it is
          // exactly when such a hidden session becomes joinable again.
          setLiveSessions((prev) =>
            prev.some((s) => s.sid === e.sid)
              ? prev.map((s) => (s.sid === e.sid ? entry(e) : s))
              : [...prev, entry(e)],
          )
        }
      },
    })
    return () => {
      try { discoveryRef.current?.close() } catch { /* swallow */ }
      discoveryRef.current = null
    }
  }, [])

  const q = searchParams.get('q') ?? ''
  const [inputQ, setInputQ] = useState(q)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // The search text waiting on the debounce, or null when none is pending.
  const pendingQRef = useRef<string | null>(null)
  // Every q this page wrote to the URL itself that the router has not handed
  // back yet, oldest first (see the q -> inputQ sync below).
  const ownQWritesRef = useRef<string[]>([])

  // Write the next query string, built from the live URL rather than from
  // `searchParams`. The router commits a URL change in a later render, so the
  // `searchParams` a handler or timer closed over can be one or more updates
  // behind: building from it would silently undo whatever landed in between
  // (a second quick filter click would drop the first; the search debounce
  // would revert a tab / sort / filter change made while it was pending). A pending search
  // is folded into the same write, so the two can't race each other either.
  const updateParams = useCallback((updater: (sp: URLSearchParams) => void) => {
    const next = new URLSearchParams(window.location.search)
    const pendingQ = pendingQRef.current
    if (pendingQ !== null) {
      if (debounceRef.current) clearTimeout(debounceRef.current)
      debounceRef.current = null
      pendingQRef.current = null
      if (pendingQ) next.set('q', pendingQ)
      else next.delete('q')
      ownQWritesRef.current.push(pendingQ)
    }
    updater(next)
    next.delete('page')
    next.delete('per_page')
    setSearchParams(next, { replace: true })
  }, [setSearchParams])

  const setSearchQ = useCallback((value: string) => {
    setInputQ(value)
    pendingQRef.current = value
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => updateParams(() => {}), 300)
  }, [updateParams])

  // Mirror a q that arrives from outside (back / forward, a link) into the
  // search box. The page's own writes come back through here too, but by the
  // time the router commits one the user may have typed further, so those
  // are recognised and skipped instead of reverting the box. An outside
  // change also drops any half-typed search, which would otherwise be
  // written over the navigation a moment later.
  useEffect(() => {
    const own = ownQWritesRef.current
    const i = own.indexOf(q)
    if (i >= 0) {
      own.splice(0, i + 1)
      return
    }
    own.length = 0
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = null
    pendingQRef.current = null
    setInputQ(q)
  }, [q])
  // Clear the pending debounce on unmount so a setSearchParams call doesn't
  // fire after the component is gone (which would log a React warning and
  // attempt to mutate URL state for a route the user has already left).
  useEffect(() => () => { if (debounceRef.current) clearTimeout(debounceRef.current) }, [])

  const sortRaw = searchParams.get('sort') ?? defaultSortField
  const sort: SortField = (SORT_OPTIONS as readonly string[]).includes(sortRaw)
    ? (sortRaw as SortField)
    : defaultSortField
  const order = (searchParams.get('order') ?? 'asc') as Order
  const typeRaw = searchParams.get('type')
  const type: IndexTypeFilter = (TYPE_OPTIONS as readonly string[]).includes(typeRaw ?? '')
    ? (typeRaw as IndexTypeFilter)
    : defaultIndexType

  // The facets with the active view's counts: a value tours carry counts in
  // "All" and "Tours" (which match through tours) but not in "Slides", and
  // one only slides carry counts in "All" and "Slides" but not in "Tours"
  // (which filters on a tour's own metadata).
  const viewFacets = useMemo(() => facetsForView(facets, type), [facets, type])

  // Only the admin-allowed filter categories appear in the panel, in the
  // order the admin set. Categories with no non-empty value on any
  // published slide are still hidden so the panel doesn't show a dead
  // filter — but since /api/facets layers in registered-but-unused
  // MetadataOption rows with count=0, "the group exists" doesn't imply
  // "there's a real value to filter on"; check that at least one entry
  // has a positive count in the active view. A category with a value
  // selected stays regardless, so its chip can still be turned off: one only
  // tours carry counts 0 in "Slides" (one only slides carry, in "Tours"),
  // and dropping it there would leave the filter on with no chip to clear it.
  const groupKeys = useMemo(
    () => visibleFilterKeys(filterAttrs, viewFacets, searchParams),
    [viewFacets, filterAttrs, searchParams],
  )

  // Counted straight off the URL rather than off groupKeys. A filter stays in
  // the query string — and keeps narrowing the results request, which
  // forwards searchParams verbatim — even after its category drops out of
  // groupKeys, which happens whenever an admin removes it from
  // index_filter_attrs or its last matching slide is unpublished. Counting per
  // visible group would report "0 filters" while the list is still filtered, so
  // a bookmarked ?stain=HE would just look like missing slides.
  const activeFilterCount = useMemo(() => {
    let sum = 0
    BASE_KEYS.forEach((key) => { sum += searchParams.getAll(key).filter(Boolean).length })
    return sum + searchParams.getAll('facet').filter(Boolean).length
  }, [searchParams])

  const hasQ = q.trim() !== ''

  // The server already searched, filtered, sorted and capped each view's
  // result (tour-title matches included in "All"), so it is rendered as-is.
  const displayedSlides = type === 'tours' ? [] : slides
  const displayedTours = type === 'tours' ? tourResults : []

  const toursBySlide = useMemo(() => groupToursBySlide(allTours), [allTours])
  // Each "All" card's inline tours, the search's matched tours first (see
  // inlineTours).
  const inlineToursBySlide = useMemo(() => {
    const m = new Map<number, TourSummary[]>()
    if (type !== 'all') return m
    for (const s of slides) m.set(s.id, inlineTours(s, toursBySlide.get(s.id)))
    return m
  }, [type, slides, toursBySlide])

  const totalToursVisible = type === 'tours'
    ? displayedTours.length
    : displayedSlides.reduce((sum, s) => sum + (inlineToursBySlide.get(s.id)?.length || 0), 0)

  // One request per view, so the active view's own total says whether its
  // cap cut the match set short.
  const truncationShown = type === 'tours' ? tourResults.length : slides.length
  const truncationTotal = type === 'tours' ? tourResultsTotal : slidesTotal
  const isTruncated = truncationTotal > truncationShown

  useEffect(() => {
    const ctrl = new AbortController()
    getFacets({ signal: ctrl.signal })
      .then(setFacets)
      .catch((err) => { if (!isAbortError(err)) { logger.error(err); setCatalogLoadError(true) } })
    return () => ctrl.abort()
  }, [])
  // The "All" view's inline tour lists, fetched the first time that view is
  // shown (after settings, which can make another view the default) and kept.
  useEffect(() => {
    if (!settingsLoaded || type !== 'all' || allToursLoaded) return
    const ctrl = new AbortController()
    listAllPublishedTours({ signal: ctrl.signal })
      .then((ts) => {
        if (ctrl.signal.aborted) return
        setAllTours(ts)
        setAllToursLoaded(true)
      })
      .catch((err) => { if (!isAbortError(err)) { logger.error(err); setCatalogLoadError(true) } })
    return () => ctrl.abort()
  }, [settingsLoaded, type, allToursLoaded])
  useEffect(() => {
    // Abortable like its sibling fetches above.
    const ctrl = new AbortController()
    getViewerSettings({ signal: ctrl.signal }).then(vs => {
      setSiteTitle(vs.site_title || '')
      setFooterText(vs.footer_text || '')
      const dt = vs.default_index_type
      if (dt && (TYPE_OPTIONS as readonly string[]).includes(dt)) {
        setDefaultIndexType(dt)
      }
      const dsf = vs.default_sort_field
      if (dsf && (SORT_OPTIONS as readonly string[]).includes(dsf)) {
        setDefaultSortField(dsf as SortField)
      }
      setFilterSortOrder(vs.filter_sort_order === 'alpha' ? 'alpha' : 'count')
      setFiltersOpen(vs.show_filter_button ?? false)
      if (!safeGetItem(VIEW_MODE_KEY)) {
        setViewMode(vs.default_view_mode === 'list' ? 'list' : 'tile')
      }
      if (!safeGetItem(THUMBNAIL_SIZE_KEY)) {
        const dts = vs.default_thumbnail_size
        if (dts === 'sm' || dts === 'md' || dts === 'lg') setThumbnailSize(dts)
      }
      if (typeof vs.max_index_items === 'number') {
        setMaxIndexItems(clampMaxIndexItems(vs.max_index_items))
      }
      // Guarded on the type, not on truthiness: 0 is a meaningful value here
      // ("list none inline") and `if (vs.index_tours_per_slide)` would silently
      // discard it and leave the hardcoded default in place.
      if (typeof vs.index_tours_per_slide === 'number') {
        setToursPerSlide(clampIndexToursPerSlide(vs.index_tours_per_slide))
      }
      if (Array.isArray(vs.index_card_attrs)) setCardAttrs(vs.index_card_attrs)
      if (Array.isArray(vs.index_filter_attrs)) setFilterAttrs(vs.index_filter_attrs)
      setShowAdminLink(!!vs.show_admin_link)
      setSeasonalBackgrounds(vs.seasonal_backgrounds || [])
      setHeaderDefaultImageUrl(vs.header_default_image_url || '')
      setHeaderDefaultTitleColor(vs.header_default_title_color || '')
    }).catch(() => {}).finally(() => setSettingsLoaded(true))
    // Fetch viewer settings exactly once on mount. The document title (the only
    // locale-dependent piece) is split into its own effect below, so this effect
    // doesn't key on `t`: keying on `t` would re-fetch and re-apply server
    // defaults on every locale change / async-bundle resolve — e.g. slamming the
    // filter panel shut after the user opened it.
    return () => ctrl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // The document title is the only piece that depends on the locale, so it
  // lives in its own effect and reacts to the fetched site title + `t`.
  useEffect(() => {
    document.title = siteTitle || t('index.siteTitleFallback')
  }, [siteTitle, t])

  const headerState = useMemo(() => {
    return pickActiveHeaderState(new Date(), seasonalBackgrounds, {
      imageUrl: headerDefaultImageUrl,
      titleColor: headerDefaultTitleColor,
    })
  }, [seasonalBackgrounds, headerDefaultImageUrl, headerDefaultTitleColor])

  useEffect(() => {
    // Wait for the real viewer settings before the first fetch: the default
    // view, defaultSortField and maxIndexItems flip from their hardcoded
    // defaults once settings resolve, so fetching before then would issue a
    // redundant request (up to maxIndexItems rows, possibly to the wrong
    // endpoint) on a non-default deployment. settingsLoaded is set on both
    // success and failure, so a settings outage still runs a single default
    // fetch rather than leaving the page stuck loading.
    if (!settingsLoaded) return
    // One request for the active view only. `q` reaches the URL — and so this
    // effect — through setSearchQ's debounce, and the abort below cancels a
    // superseded keystroke's request, so only the latest search lands.
    const ctrl = new AbortController()
    const opts = { defaultSort: defaultSortField, cap: maxIndexItems }
    // Within one view, the previous search's results stay up while the next
    // one loads (no flicker while typing). Across a view switch they are
    // dropped: the view being entered last fetched for whatever query was
    // current when it was left, possibly long ago, and showing that under
    // the new query — with its stale count and truncation banner, and
    // indefinitely if this request fails — would misreport the search.
    if (lastResultsViewRef.current !== type) {
      if (type === 'tours') {
        setTourResults([])
        setTourResultsTotal(0)
      } else {
        setSlides([])
        setSlidesTotal(0)
      }
    }
    lastResultsViewRef.current = type
    setResultsLoadError(false)
    setResultsPending(true)
    const onError = (err: unknown) => {
      if (isAbortError(err)) return
      logger.error(err)
      setResultsLoadError(true)
      setResultsPending(false)
    }
    if (type === 'tours') {
      listTourIndex(tourIndexParams(searchParams, opts), { signal: ctrl.signal }).then((res) => {
        if (ctrl.signal.aborted) return
        setTourResults(res.items)
        setTourResultsTotal(res.total)
        setSlideLabels((prev) => rememberSlideLabels(prev, res.items.map((it) => it.slide)))
        setResultsPending(false)
      }).catch(onError)
    } else {
      listSlides(slideListParams(searchParams, type, opts), { signal: ctrl.signal }).then((res) => {
        if (ctrl.signal.aborted) return
        setSlides(res.items)
        setSlidesTotal(res.total)
        setSlideLabels((prev) => rememberSlideLabels(prev, res.items))
        setResultsPending(false)
      }).catch(onError)
    }
    return () => ctrl.abort()
  }, [searchParams, type, defaultSortField, maxIndexItems, settingsLoaded])

  const toggleFilter = (key: string, value: string) => {
    updateParams((sp) => {
      if (BASE_KEYS.has(key)) {
        const values = new Set(sp.getAll(key))
        values.has(value) ? values.delete(value) : values.add(value)
        sp.delete(key)
        Array.from(values).sort().forEach(v => sp.append(key, v))
      } else {
        const token = `${key}:${value}`
        const values = new Set(sp.getAll('facet'))
        values.has(token) ? values.delete(token) : values.add(token)
        sp.delete('facet')
        Array.from(values).sort().forEach(v => sp.append('facet', v))
      }
    })
  }

  const clearAllFilters = () => {
    updateParams((sp) => {
      // Every base key, not just the ones currently shown as groups: a base
      // filter whose category has since left groupKeys is still narrowing the
      // results, and clearing only the visible ones would leave the user with
      // no way to get rid of it (see activeFilterCount). 'facet' is cleared
      // wholesale for the same reason.
      BASE_KEYS.forEach((key) => sp.delete(key))
      sp.delete('facet')
    })
  }

  const bannerSessions: LiveSessionForBanner[] = useMemo(
    () => toBannerSessions(liveSessions, slideLabels),
    [liveSessions, slideLabels],
  )

  return (
    <div className="app-viewport" style={{ background: 'var(--color-bg)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {/* Top stack: the live-sync join banner and the header. Only <main>
          below scrolls, so this stack (and the footer) stay pinned to the
          viewport without `position: fixed`. The banner caps its own height
          and scrolls internally when many presenters are live, so this stack
          can't grow to swallow the page. */}
      <div style={{ flexShrink: 0, zIndex: 100 }}>
        <LiveSessionBanner sessions={bannerSessions} />

        <IndexHeader
        siteTitle={siteTitle}
        titleBarBackgroundUrl={headerState.imageUrl}
        titleColor={headerState.titleColor}
        inputQ={inputQ}
        onSearchChange={setSearchQ}
        sort={sort}
        order={order}
        type={type}
        defaultIndexType={defaultIndexType}
        viewMode={viewMode}
        onViewModeChange={switchViewMode}
        thumbnailSize={thumbnailSize}
        onThumbnailSizeChange={switchThumbnailSize}
        groupKeys={groupKeys}
        facets={viewFacets}
        filterSortOrder={filterSortOrder}
        searchParams={searchParams}
        filtersOpen={filtersOpen}
        onFiltersToggle={() => setFiltersOpen(v => !v)}
        activeFilterCount={activeFilterCount}
        displayedSlides={displayedSlides}
        displayedTours={displayedTours}
        totalToursVisible={totalToursVisible}
        onUpdateParams={updateParams}
        onToggleFilter={toggleFilter}
        onClearFilters={clearAllFilters}
        onAbout={() => setShowAbout(true)}
        showAdminLink={showAdminLink}
        />
      </div>

      {loadError && (
        <div
          role="alert"
          style={{
            flexShrink: 0,
            padding: '8px 24px',
            background: 'var(--color-danger-bg, #fef2f2)',
            borderBottom: '1px solid var(--color-danger-border, #fecaca)',
            color: 'var(--color-danger-fg, #991b1b)',
            fontSize: 'var(--font-size-sm)',
          }}
        >
          {t('index.loadError')}
        </div>
      )}

      {isTruncated && (
        <div
          role="status"
          style={{
            flexShrink: 0,
            padding: '8px 24px',
            background: 'var(--color-warning-bg)',
            borderBottom: '1px solid var(--color-warning-border)',
            color: 'var(--color-warning-fg)',
            fontSize: 'var(--font-size-sm)',
          }}
        >
          {t(type === 'tours' ? 'index.truncatedBannerTours' : 'index.truncatedBanner', { shown: truncationShown, total: truncationTotal })}
        </div>
      )}

      <main style={{ flex: 1, minHeight: 0, overflowY: 'auto', WebkitOverflowScrolling: 'touch', padding: '24px 24px' }}>
        {type === 'tours' ? (
          displayedTours.length === 0 ? (
            resultsPending ? null : <EmptyState message={t('index.noTours')} />
          ) : viewMode === 'tile' ? (
            <div style={{
              display: 'grid',
              gridTemplateColumns: `repeat(auto-fill, minmax(${THUMBNAIL_TILE_SIZES[thumbnailSize].gridMin}px, 1fr))`,
              gap: 16,
            }}>
              {displayedTours.map(tour => (
                <TourTile key={tour.id} tour={tour} slide={tour.slide} cardAttrs={cardAttrs} thumbHeight={THUMBNAIL_TILE_SIZES[thumbnailSize].thumbHeight} />
              ))}
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {displayedTours.map(tour => (
                <TourRow key={tour.id} tour={tour} slide={tour.slide} cardAttrs={cardAttrs} variant="flat" thumbWidth={THUMBNAIL_LIST_SIZES[thumbnailSize].thumbWidth} thumbHeight={THUMBNAIL_LIST_SIZES[thumbnailSize].thumbHeight} />
              ))}
            </div>
          )
        ) : displayedSlides.length === 0 ? (
          resultsPending ? null : <EmptyState message={hasQ ? t('index.noResults') : t('index.noSlides')} />
        ) : viewMode === 'tile' ? (
          <div style={{
            display: 'grid',
            gridTemplateColumns: `repeat(auto-fill, minmax(${THUMBNAIL_TILE_SIZES[thumbnailSize].gridMin}px, 1fr))`,
            // Cards size to their own content whenever tours are inlined.
            // Grid's default `align-items: stretch` makes every card in a row
            // as tall as the tallest one, so a card whose tour list is
            // expanded (or that simply carries more tours than its neighbours)
            // drags hundreds of pixels of blank surface under every sibling in
            // its row. Without inline tours the height spread is at most a
            // wrapped metadata line, and the uniform-height row reads tidier,
            // so stretch is kept for those views.
            alignItems: type === 'all' && totalToursVisible > 0 ? 'start' : 'stretch',
            gap: 16,
          }}>
            {displayedSlides.map(s => (
              <SlideCard
                key={s.id}
                slide={s}
                tours={inlineToursBySlide.get(s.id) || []}
                thumbHeight={THUMBNAIL_TILE_SIZES[thumbnailSize].thumbHeight}
                cardAttrs={cardAttrs}
                toursPerSlide={inlineTourCap(s, toursPerSlide)}
              />
            ))}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {displayedSlides.map(s => (
              <SlideListItem
                key={s.id}
                slide={s}
                tours={inlineToursBySlide.get(s.id) || []}
                thumbWidth={THUMBNAIL_LIST_SIZES[thumbnailSize].thumbWidth}
                thumbHeight={THUMBNAIL_LIST_SIZES[thumbnailSize].thumbHeight}
                cardAttrs={cardAttrs}
                toursPerSlide={inlineTourCap(s, toursPerSlide)}
              />
            ))}
          </div>
        )}
      </main>

      {showAbout && <AboutDialog onClose={() => setShowAbout(false)} />}

      {footerText && (
        // The footer is a normal flex child at the bottom of the fixed-height
        // shell, so it stays glued to the viewport bottom while <main> scrolls
        // internally. This avoids the WebKit/iOS bug where a `position: fixed`
        // footer detaches from the viewport during rubber-band overscroll
        // (observed on iPad when scrolling the slide list to the top).
        <footer style={{
          flexShrink: 0,
          borderTop: '1px solid var(--color-border)',
          background: 'var(--color-surface)',
          padding: '10px 24px',
          fontSize: 'var(--font-size-sm)',
          color: 'var(--color-text-subtle)',
          textAlign: 'center',
          whiteSpace: 'pre-wrap',
        }}>
          {footerText}
        </footer>
      )}
    </div>
  )
}

