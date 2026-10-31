import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router'
import OpenSeadragon from 'openseadragon'
import { OSD_PREFIX_URL, osdMaxTilesPerFrame, applyStepTransition, attachHomeResetsRotation, attachNoFlipKey, attachOpenFailed, attachTwoFingerRotate, attachZoomClamp, loadTileSource } from '../lib/osd'
import {
  DEFAULT_VIEWER_SETTINGS,
  getSlide,
  bearerHeaders,
  getToken,
  getTour,
  getViewerSettings,
  isAbortError,
  isRateLimitError,
  isStaffFromToken,
  isSziSlide,
  normalizeTransitionMode,
  Slide,
  TourStep,
  TourSummary,
  type TransitionMode,
  ViewerSettings,
} from '../lib/api'
import { logger } from '../lib/logger'
import { applyGraphicCounterRotation, applyLabelCounterRotation, createPinOverlay, normalizePinShape, pinPlacement } from '../lib/tourPins'
import { applyShapeLabelTransforms, createShapeOverlay, normalizeShape, type ShapeOverlay } from '../lib/tourShapes'
import { orderAnnotations } from '../lib/tourLayers'
import { stepMpp, stepViewportBounds } from '../lib/tourStepView'
import SafeMarkdown from '../components/SafeMarkdown'
import AboutDialog from '../components/AboutDialog'
import SlideCredit, { ViewerInfoCorner } from '../components/SlideCredit'
import SlideInfoPanel from '../components/SlideInfoPanel'
import MouseCoordsOverlay from '../components/MouseCoordsOverlay'
import DebugToggle from '../components/DebugToggle'
import SignOutButton from '../components/SignOutButton'
import InfoIcon from '../components/icons/InfoIcon'
import ImageAdjustControls from '../components/ImageAdjustControls'
import { DEFAULT_IMAGE_ADJUST, ImageAdjust, cssFilterString, imageAdjustFromSlide } from '../lib/imageAdjust'
import { Button } from '../components/ui'
import { useI18n } from '../lib/i18n'
import { useAuthGate } from '../lib/useAuthGate'
import { useTourSession } from '../hooks/useTourSession'
import { useViewportHeight } from '../hooks/useViewportHeight'
import { useMediaQuery } from '../hooks/useMediaQuery'
import { NO_PINCH_ZOOM_CLASS, usePreventPinchZoom } from '../hooks/usePreventPinchZoom'
import { useNoOverscroll } from '../hooks/useNoOverscroll'
import { useViewerOverlays } from '../hooks/useViewerOverlays'
import { useDebugParam } from '../hooks/useDebugParam'
import { useMouseCoords } from '../hooks/useMouseCoords'

// The tour data as last fetched, tagged with the route tokens it was fetched
// for. Tour and steps live in one state object so they can never belong to two
// different tours, and the tag lets a render tell "loaded for this route" apart
// from "left over from the previous route" (see `tourMatchesRoute`).
type LoadedTour = {
  slidePublicId: string
  tourPublicId: string
  tour: TourSummary
  steps: TourStep[]
}
// Stable empty list for the not-loaded case, so `steps` keeps one identity
// across renders and doesn't re-fire the effects / callbacks keyed on it.
const NO_STEPS: TourStep[] = []

export default function TourViewerPage() {
  useAuthGate()
  const { id, tourId } = useParams()
  const navigate = useNavigate()
  // URL segments are the slide/tour public_id tokens, not numeric PKs. They
  // key the public fetches, the tile source, analytics and the canonical
  // redirect. (The tour viewer is view-only — unlike ViewerPage it makes no
  // authenticated admin calls — so the loaded rows' numeric ids are used only
  // internally, e.g. as effect dependencies.)
  const slidePublicId = id ?? ''
  const tourPublicId = tourId ?? ''

  const { t } = useI18n()
  const appHeight = useViewportHeight()
  // Phone portrait (and other narrow windows): the step panel alone is wider
  // than the screen, which would squeeze the slide pane (flex: 1, basis 0) to zero
  // width. Below this breakpoint the panel stacks under the slide instead;
  // above it, the panel sits beside the slide (width: see the <aside> below).
  const stacked = useMediaQuery('(max-width: 720px)')
  const [slide, setSlide] = useState<Slide | null>(null)
  // Initial state must populate every field on ViewerSettings so the OSD
  // init effect's numeric dependencies (notably max_zoom_pixel_ratio)
  // don't transition from undefined → number when the settings fetch
  // resolves and force a viewer rebuild.
  const [viewerSettings, setViewerSettings] = useState<ViewerSettings>(DEFAULT_VIEWER_SETTINGS)
  // Whether getViewerSettings() has resolved (or failed) at least once. The
  // OSD-init effect below bakes several settings into the viewer at
  // construction (max_zoom_pixel_ratio, immediate_render, max_image_cache_
  // count) and also lists them in its dependency array, so building with the
  // hardcoded defaults and then letting the real settings arrive would destroy
  // and rebuild the freshly built viewer (black flash + duplicate tile fetches
  // + viewport snap back to step 0). Gate the first build on the settings being
  // known so the viewer is built once, with the real values. Never reset to
  // false — settings are global and load once — so a later slide change doesn't
  // re-gate. Mirrors ViewerPage.
  const [viewerSettingsLoaded, setViewerSettingsLoaded] = useState(false)
  const showZoomIndicator = slide?.show_zoom_indicator ?? viewerSettings.show_zoom_indicator
  // Honour the per-slide override, falling back to the global setting — same
  // precedence as ViewerPage — so a slide/site that hides the scale bar or the
  // metadata card in the regular viewer hides them in the tour viewer too.
  const showScaleBar = slide?.show_scale_bar ?? viewerSettings.show_scale_bar
  const showMetadataPanel = slide?.show_metadata_panel ?? viewerSettings.show_metadata_panel
  const [loadedTour, setLoadedTour] = useState<LoadedTour | null>(null)
  // On an in-place tour change (browser back/forward between two tour URLs
  // this page stays mounted for) `tourPublicId` updates in a render that still
  // holds the previous tour's data — and `currentIdx` still points into it.
  // Everything downstream reads `tour` / `steps` derived here, so in that
  // render (and until the new tour's fetch lands) they read as "not loaded"
  // rather than as the old tour's. Clearing the old data from an effect would
  // be too late: effects declared earlier in this render (useTourSession's,
  // above all) have already run against it. The slide token is part of the
  // match because the tour effect refetches on either token (and may redirect
  // to the tour's canonical slide), so data for another slide's URL is not
  // this route's data either.
  const tourMatchesRoute = loadedTour !== null
    && loadedTour.tourPublicId === tourPublicId
    && loadedTour.slidePublicId === slidePublicId
  const tour = tourMatchesRoute ? loadedTour.tour : null
  const steps = tourMatchesRoute ? loadedTour.steps : NO_STEPS
  const [currentIdx, setCurrentIdx] = useState(0)
  const [showAnnotations, setShowAnnotations] = useState(true)
  // Error banner shown when slide / tour metadata fetch fails. Without this
  // the viewer would just silently render an empty pane. `loadError` holds an
  // i18n key for slide-fetch / OSD-init failures; the tour fetch keeps its own
  // flag so each effect only ever clears the error it owns.
  const [loadError, setLoadError] = useState<string | null>(null)
  const [tourLoadError, setTourLoadError] = useState(false)
  const [searchParams, setSearchParams] = useSearchParams()
  const [debugMode, toggleDebug] = useDebugParam()
  const fromAdmin = searchParams.get('from') === 'admin'

  // Tour-session tracker: emits start / progress / complete beacons so
  // the admin dashboard can compute "how many users completed this tour
  // and how long did it take them". Backend silently drops when
  // analytics_mode='off' or the viewer is anonymous.
  //
  // Fed only a tour id whose data is loaded for the current route, so the
  // hook never sees a new tour id paired with the outgoing tour's step count
  // or step index: it would start the new session with the old
  // `total_steps` (and never re-send start once the real count lands), and
  // report the old index as progress — or even completion — on the new tour.
  // The outgoing session is closed out as soon as the route moves on.
  useTourSession(tourMatchesRoute ? tourPublicId : null, currentIdx, steps.length)
  // Bumped on every OSD (re)open so downstream effects (scale bar, step
  // positioning) re-run against the fresh viewer instance.
  const [osdGen, setOsdGen] = useState(0)

  const viewerDivRef = useRef<HTMLDivElement | null>(null)
  // Page-zoom gestures are cancelled across the whole page (see the root <div>
  // below), not just on the step panel.
  const pageRef = useRef<HTMLDivElement | null>(null)
  usePreventPinchZoom(pageRef)
  // No rubber-band / stretch when a side panel is dragged past its end, nor
  // of the fixed-height page itself (see useNoOverscroll).
  useNoOverscroll()
  const osdRef = useRef<OpenSeadragon.Viewer | null>(null)
  const overlayElsRef = useRef<HTMLElement[]>([])
  const labelRotatorsRef = useRef<HTMLElement[]>([])
  const graphicRotatorsRef = useRef<{ el: HTMLElement; baseRot: number }[]>([])
  const shapeOverlaysRef = useRef<ShapeOverlay[]>([])
  // Tracks the previous step index so the source step's
  // transition_mode can be looked up when navigating between
  // steps. null means no step has been applied yet (first paint should
  // always be immediate).
  const prevStepIdxRef = useRef<number | null>(null)
  const [showAbout, setShowAbout] = useState(false)
  // Image adjustment (brightness/contrast/saturation). Seeded from the slide's
  // saved preset on load and persists across tour step changes; further tweaks
  // are session-only and revert to the preset on slide/route change or reload.
  const [imageAdjust, setImageAdjust] = useState<ImageAdjust>(DEFAULT_IMAGE_ADJUST)

  useEffect(() => {
    if (!slidePublicId) return
    const ctrl = new AbortController()
    setLoadError(null)
    // Same admin-preview rationale as ViewerPage: forward the admin JWT (if
    // any) so the public token-keyed endpoint serves drafts to admins.
    const adminToken = (fromAdmin && isStaffFromToken()) ? getToken() : null
    const slideP = getSlide(slidePublicId, { signal: ctrl.signal, adminToken })
    slideP.then((s) => {
      setSlide(s)
      // Seed the adjustment from the slide's saved preset (same as the regular
      // viewer). Tours are view-only for presets — no in-tour save affordance.
      if (s) setImageAdjust(imageAdjustFromSlide(s))
    }).catch((err) => {
      if (isAbortError(err)) return
      logger.error('failed to load slide', err)
      // Drop the prior slide so a failed navigation doesn't leave the old
      // slide's image on screen beneath the error banner (the OSD-init guard
      // keys on slide.public_id, so clearing it also tears the viewer down).
      setSlide(null)
      // An i18n key, not err.message (an English fallback, an internal code
      // like "rate-limited", or a raw nginx HTML body on a 502).
      setLoadError(isRateLimitError(err) ? 'common.rateLimited' : 'viewer.slideLoadFailed')
    })
    getViewerSettings({ signal: ctrl.signal })
      .then((s) => { setViewerSettings(s); setViewerSettingsLoaded(true) })
      .catch((err) => {
        if (isAbortError(err)) return
        logger.warn('failed to load viewer settings', err)
        // Unblock OSD init on failure too so a settings outage doesn't leave
        // the viewer permanently unbuilt — it just uses the built-in defaults.
        setViewerSettingsLoaded(true)
      })
    return () => ctrl.abort()
  }, [slidePublicId, fromAdmin])

  useEffect(() => {
    if (!tourPublicId) return
    const ctrl = new AbortController()
    // Clear any prior tour-fetch error so a stale "failed to load tour"
    // banner doesn't stick around when the user navigates from a broken
    // tour to a working one on the same slide. Separate from `loadError`, so
    // this can't wipe a slide/OSD error and the OSD-init reset can't wipe it.
    setTourLoadError(false)
    // Reset the previous-step tracker so the first step transition in the
    // newly-loaded tour doesn't read transition_mode off a stale step
    // index from the previous tour. Without this, the inherited prevIdx
    // points into the *new* steps array, picking up some unrelated step's
    // transition mode.
    prevStepIdxRef.current = null
    // No need to drop the outgoing tour's steps here: `tour` / `steps` are
    // derived from `loadedTour` only while its tag matches the route (see
    // `tourMatchesRoute`), so the gap until this fetch lands already renders
    // as the same empty state as a first load — every consumer below guards
    // on `steps.length`.
    const adminToken = (fromAdmin && isStaffFromToken()) ? getToken() : null
    const tourP = getTour(tourPublicId, { signal: ctrl.signal, adminToken })
    tourP.then((d) => {
      // The route accepts the slide token independently of the tour's actual
      // parent slide, so a hand-crafted URL like /slides/A/tours/B (where tour
      // B belongs to slide C) would otherwise render slide A's image with slide
      // C's level-0 pin/shape coordinates overlaid on top. Redirect to the
      // canonical URL for the tour's real slide (both keyed by public_id).
      // Preserve the query string (notably ?from=admin) so an admin who
      // hand-crafted the URL keeps the draft-preview affordance.
      if (d.tour.slide_public_id && d.tour.slide_public_id !== slidePublicId) {
        const search = window.location.search || ''
        navigate(
          `/slides/${encodeURIComponent(d.tour.slide_public_id)}/tours/${encodeURIComponent(d.tour.public_id)}${search}`,
          { replace: true },
        )
        return
      }
      // Tagged with the tokens this effect fetched for (the route's), which is
      // what `tourMatchesRoute` compares against. Set in the same batch as
      // `currentIdx` below, so the first render that sees this tour's steps
      // also sees its own start index rather than the previous tour's.
      setLoadedTour({ slidePublicId, tourPublicId, tour: d.tour, steps: d.steps || [] })
      // Deep-link: open at the step named by ?step=N (1-based), clamped to the
      // valid range; anything missing/invalid falls back to the first step.
      // Read from window.location.search (not the searchParams dep) so a later
      // step change — which rewrites ?step — doesn't re-trigger this fetch.
      const stepRaw = Number(new URLSearchParams(window.location.search).get('step'))
      const count = d.steps?.length || 0
      const startIdx = (Number.isFinite(stepRaw) && stepRaw >= 1 && count > 0)
        ? Math.min(Math.round(stepRaw) - 1, count - 1)
        : 0
      setCurrentIdx(startIdx)
      if (d.tour?.title) document.title = d.tour.title
    }).catch((err) => {
      if (isAbortError(err)) return
      // Drop the prior tour's steps/overlays so a failed navigation to a
      // broken or deleted tour doesn't keep rendering the old tour's step list
      // and annotations as if they were still valid. (The route tag already
      // hides them; clearing also releases the stale data.)
      setLoadedTour(null)
      setTourLoadError(true)
    })
    return () => ctrl.abort()
  }, [tourPublicId, slidePublicId, navigate, fromAdmin])

  // Wait for the slide metadata before opening OSD: SZI tile sources need an
  // async fetch of the archive descriptor, so the format must be known
  // before constructing the viewer. The non-SZI path is also funnelled
  // through the same code so the lifecycle stays uniform.
  const isSzi = isSziSlide(slide)
  useEffect(() => {
    if (!viewerDivRef.current) return
    if (!slide || slide.public_id !== slidePublicId) return
    // Wait for the real viewer settings before the first build so the viewer
    // isn't constructed with the default max_zoom_pixel_ratio / immediate_render
    // / max_image_cache_count and then torn down + rebuilt when they resolve.
    if (!viewerSettingsLoaded) return
    let cancelled = false
    // Admin draft tour previews need the same Authorization header pass-through
    // as ViewerPage so the /dzi/ publish gate accepts draft tile fetches.
    const adminToken = (fromAdmin && isStaffFromToken()) ? getToken() : null
    const ajaxHeaders = adminToken ? bearerHeaders(adminToken) : undefined
    // The WSI generation versions every tile URL (see lib/tileUrl); it is in
    // the deps below so a replaced slide re-opens on its new URLs.
    const tileVersion = slide.tile_version
    let cleanup: (() => void) | null = null
    ;(async () => {
      const result = await loadTileSource(slidePublicId, isSzi, ajaxHeaders, tileVersion)
      if (cancelled || !viewerDivRef.current) return
      if (!result.ok) {
        // Surface the failure so the user sees something other than a
        // black canvas. Mirrors ViewerPage's handling of the same path.
        setLoadError(
          result.reason === 'szi-plugin-missing'
            ? 'viewer.sziPluginMissing'
            : 'viewer.sziOpenFailed'
        )
        return
      }
      // Drop any stale OSD-induced loadError from a prior re-init so a
      // recovered SZI fetch doesn't leave the error banner pinned over a
      // healthy viewer. Slide-fetch failures aren't reachable here, and the
      // tour-fetch error lives in its own state.
      setLoadError(null)
      const maxZoomPixelRatio = typeof viewerSettings.max_zoom_pixel_ratio === 'number'
        ? viewerSettings.max_zoom_pixel_ratio
        : 1.0
      const v = OpenSeadragon({
        element: viewerDivRef.current,
        prefixUrl: OSD_PREFIX_URL,
        tileSources: result.tileSource,
        showRotationControl: true,
        showNavigator: true,
        maxZoomPixelRatio,
        // Fetch display-resolution tiles first, skipping in-between levels
        // (see ViewerPage for the trade-off). Admin-tunable via Advanced
        // rendering settings (default true).
        immediateRender: viewerSettings.immediate_render,
        maxImageCacheCount: viewerSettings.max_image_cache_count,
        maxTilesPerFrame: osdMaxTilesPerFrame(),
        gestureSettingsMouse: { clickToZoom: false },
        // Rotation is handled by attachTwoFingerRotate, not OSD's
        // distance-gated pinchRotate (see the helper for why).
        ...(ajaxHeaders ? { loadTilesWithAjax: true, ajaxHeaders } : {}),
        debugMode,
      })
      osdRef.current = v
      // A DZI descriptor that fails to load gets a translated banner instead
      // of OSD's raw English message (see attachOpenFailed).
      const detachOpenFailed = attachOpenFailed(v, () => setLoadError('viewer.slideLoadFailed'))
      const detachZoomClamp = attachZoomClamp(v)
      const detachRotate = attachTwoFingerRotate(v)
      // Make the Go Home button a full reset (pan + zoom + rotation).
      const detachHomeReset = attachHomeResetsRotation(v)
      // No mirror-image slide from a stray ``f`` key.
      const detachNoFlip = attachNoFlipKey(v)
      v.addOnceHandler('open', () => setOsdGen(g => g + 1))

      // Track viewport updates (rotate + zoom + pan) so pin labels and arrows
      // stay correctly oriented as the slide spins, and shape labels/badges
      // keep tracking each shape's current rotated AABB in screen pixels.
      // rAF-coalesce so multiple synchronous viewport-mutating callers in the
      // same frame produce a single DOM read/write pass, matching the
      // pattern in useTourViewer.
      let rafId = 0
      const onViewportUpdate = () => {
        if (rafId) return
        rafId = requestAnimationFrame(() => {
          rafId = 0
          const rot = v.viewport?.getRotation() || 0
          for (const el of labelRotatorsRef.current) applyLabelCounterRotation(el, rot)
          for (const { el, baseRot } of graphicRotatorsRef.current) applyGraphicCounterRotation(el, baseRot, rot)
          for (const overlay of shapeOverlaysRef.current) applyShapeLabelTransforms(overlay, rot)
        })
      }
      v.addHandler('update-viewport', onViewportUpdate)

      cleanup = () => {
        detachOpenFailed()
        detachZoomClamp()
        detachRotate()
        detachHomeReset()
        detachNoFlip()
        v.removeHandler('update-viewport', onViewportUpdate)
        if (rafId) { cancelAnimationFrame(rafId); rafId = 0 }
        v.destroy()
        // Clear the ref so the next effect run, and any concurrent
        // step/overlay effects, don't operate on a destroyed viewer.
        // Without this, the step effect's `if (!osdRef.current) return`
        // guard fails open and OSD throws on viewport access.
        if (osdRef.current === v) osdRef.current = null
      }
    })()

    return () => {
      cancelled = true
      if (cleanup) cleanup()
    }
  }, [slidePublicId, debugMode, isSzi, slide?.id, slide?.tile_version, viewerSettingsLoaded, viewerSettings.max_zoom_pixel_ratio, viewerSettings.immediate_render, viewerSettings.max_image_cache_count, fromAdmin])

  const { scaleBarDivRef, zoomIndicatorDivRef, digitalZoomBadgeDivRef } = useViewerOverlays({
    viewerRef: osdRef, slide, osdGen, showScaleBar, showZoomIndicator,
  })
  // The coords overlay only renders in debug mode, so skip the per-move
  // setState (a re-render of this whole page per mouse event) otherwise.
  const mouseCoords = useMouseCoords(osdRef, debugMode, osdGen)

  // Re-frame the viewport to a step's stored center / FOV / rotation. Shared by
  // the step-change effect below and the "reset view" button, so both apply the
  // exact same rect math as the editor (lib/tourStepView: FOV persisted in µm,
  // converted via the slide's MPP or a fixed fallback factor, rotation-aware).
  const frameStep = useCallback((idx: number, mode: TransitionMode) => {
    const viewer = osdRef.current
    if (!slide || !viewer || !steps.length) return
    const s = steps[idx]
    if (!s) return
    const vpRect = stepViewportBounds(viewer.viewport, s, stepMpp(slide))
    applyStepTransition(viewer, vpRect, s.rotation_deg || 0, mode)
  }, [slide, steps])

  // Viewport transition. Deliberately NOT keyed on showAnnotations: toggling
  // the annotations checkbox must not refit the viewport (which would discard
  // the user's free pan/zoom within the step). Only an actual step change,
  // a fresh OSD instance (osdGen), or new step data moves the viewport.
  useEffect(() => {
    if (!slide || !osdRef.current || !steps.length) return
    if (!steps[currentIdx]) return

    // The transition mode belongs to the *source* step (the one being left),
    // so a step's setting controls how the user moves OUT of it. First paint
    // and same-index re-renders use 'none' to avoid spurious animations from
    // re-mounts.
    const prevIdx = prevStepIdxRef.current
    let mode: TransitionMode = 'none'
    if (prevIdx !== null && prevIdx !== currentIdx) {
      mode = normalizeTransitionMode(steps[prevIdx]?.transition_mode)
    }
    frameStep(currentIdx, mode)
    prevStepIdxRef.current = currentIdx
  }, [slide, steps, currentIdx, osdGen, frameStep])

  // Annotation overlays. Re-runs on the annotations toggle (and step changes)
  // WITHOUT touching the viewport, so hiding/showing overlays leaves the
  // user's current framing intact.
  useEffect(() => {
    const viewer = osdRef.current
    if (!viewer) return

    // Before the guards below: a switch to another tour on the same slide
    // keeps this viewer, so with no steps to draw (the new tour still
    // loading, failed to load, or empty) the old tour's overlays must still
    // come off.
    overlayElsRef.current.forEach((el) => {
      try { viewer.removeOverlay(el) } catch {}
    })
    overlayElsRef.current = []
    labelRotatorsRef.current = []
    graphicRotatorsRef.current = []
    shapeOverlaysRef.current = []

    if (!slide || !steps.length || !showAnnotations) return
    const s = steps[currentIdx]
    if (!s) return

    // Add bottom → top in the shared z order. OSD stacks overlays by DOM
    // insertion order (overlays are wrapped in absolutely-positioned wrappers
    // with no z-index), so adding in z order is what sets the stacking: pins
    // default above shapes and any reordering saved by the editor is honoured.
    const shapes = (s.shapes_l0 || []).map(normalizeShape)
    orderAnnotations(s.pins ?? [], shapes).forEach((entry) => {
      if (entry.kind === 'pin') {
        const p = entry.item
        const shape = normalizePinShape(p.shape)
        const overlay = createPinOverlay(shape, p.color, p.rotation_deg, p.label)
        const pt = viewer.viewport.imageToViewportCoordinates(new OpenSeadragon.Point(p.x, p.y))
        viewer.addOverlay({ element: overlay.root, location: pt, placement: pinPlacement(shape) })
        overlayElsRef.current.push(overlay.root)
        if (overlay.labelRotator) labelRotatorsRef.current.push(overlay.labelRotator)
        if (overlay.graphicRotator) graphicRotatorsRef.current.push({ el: overlay.graphicRotator, baseRot: overlay.graphicBaseRotation })
      } else {
        const sh = entry.item
        const overlay = createShapeOverlay(sh, null)
        const vp = viewer.viewport.imageToViewportRectangle(new OpenSeadragon.Rect(sh.x, sh.y, sh.w, sh.h))
        viewer.addOverlay({ element: overlay.root, location: vp })
        overlayElsRef.current.push(overlay.root)
        if (overlay.labelRotator) shapeOverlaysRef.current.push(overlay)
      }
    })

    // Apply current rotation right after rendering so the initial paint matches
    // the viewport (skipping the brief frame before the next viewport update).
    const rot = viewer.viewport?.getRotation() || 0
    for (const el of labelRotatorsRef.current) applyLabelCounterRotation(el, rot)
    for (const { el, baseRot } of graphicRotatorsRef.current) applyGraphicCounterRotation(el, baseRot, rot)
    for (const overlay of shapeOverlaysRef.current) applyShapeLabelTransforms(overlay, rot)
  }, [slide, steps, currentIdx, showAnnotations, osdGen])

  // Keep the URL's ?step in sync with the current step so the address bar is
  // always a shareable deep-link to the step on screen. Step 1 leaves the param
  // off to keep the canonical tour URL clean. Guarded against re-writing an
  // already-correct value so it doesn't loop on its own searchParams update.
  useEffect(() => {
    if (!steps.length) return
    const desired = currentIdx === 0 ? null : String(currentIdx + 1)
    if (searchParams.get('step') === desired) return
    const next = new URLSearchParams(searchParams)
    if (desired === null) next.delete('step'); else next.set('step', desired)
    setSearchParams(next, { replace: true })
  }, [currentIdx, steps.length, searchParams, setSearchParams])

  const currentStep = steps[currentIdx]

  return (
    <div ref={pageRef} className={NO_PINCH_ZOOM_CLASS} style={{
      display: 'flex',
      flexDirection: stacked ? 'column' : 'row',
      height: appHeight,
      // Scrolling only, no page pinch-zoom: a tablet user pinches the slide
      // all the time, and a pinch starting just off it lands on the step panel
      // or on an overlay over the slide (info panel, credit, scale bar, …) and
      // would zoom the whole page. Browsers that honour touch-action stop that from
      // this alone, with NO_PINCH_ZOOM_CLASS repeating it on every descendant:
      // iOS Safari starts again from auto inside the scrolling step text. The
      // rest (trackpad pinch) is cancelled by usePreventPinchZoom. The slide's
      // own pinch is untouched: OpenSeadragon sets touch-action: none on its
      // element (the class leaves it alone) and reads the gesture from pointer
      // events there.
      touchAction: 'pan-x pan-y',
      background: 'var(--color-bg)',
    }}>
      <div style={{ flex: 1, position: 'relative', minWidth: 0, minHeight: 0 }}>
        <ViewerInfoCorner>
          {showMetadataPanel && <SlideInfoPanel slide={slide} settings={viewerSettings} tourAttributes={tour?.attributes} />}
          <SlideCredit key={slide?.public_id} credit={slide} />
        </ViewerInfoCorner>
        {fromAdmin && <DebugToggle enabled={debugMode} onToggle={toggleDebug} />}
        <div
          ref={viewerDivRef}
          className="osd-with-adjust"
          style={{
            width: '100%',
            height: '100%',
            ['--osd-filter' as any]: cssFilterString(imageAdjust),
          }}
        />
        {(loadError || tourLoadError) && (
          <div style={{
            position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
            zIndex: 40, background: '#fef2f2', color: '#991b1b',
            border: '1px solid #fecaca', padding: '8px 14px', borderRadius: 6,
            fontSize: 13, fontWeight: 500, maxWidth: '90%',
          }}>
            {loadError ? t(loadError) : t('viewer.tourLoadFailed')}
          </div>
        )}
        {showScaleBar && <div ref={scaleBarDivRef as any}></div>}
        {showZoomIndicator && <div ref={zoomIndicatorDivRef as any}></div>}
        <div ref={digitalZoomBadgeDivRef as any}></div>
        <ImageAdjustControls value={imageAdjust} onChange={setImageAdjust} baseline={imageAdjustFromSlide(slide)} />
        {debugMode && <MouseCoordsOverlay coords={mouseCoords} />}
      </div>

      <aside className="overscroll-panel" style={{
        // Stacked: a fixed share of the height, scrolling as a whole if the
        // header + toolbar alone outgrow it. Side by side: 30% of the window,
        // clamped to 320–420px — the full 420px from a 1400px-wide window up
        // (desktop), while a landscape tablet keeps more room for the slide
        // than a fixed 420px would leave it (over a third of an 11" iPad).
        ...(stacked
          ? { flex: '0 0 45%', borderTop: '1px solid var(--color-border)', overflow: 'auto' }
          : { flex: '0 0 clamp(320px, 30vw, 420px)', borderLeft: '1px solid var(--color-border)' }),
        // As a flex item the panel otherwise defaults to min-width: auto, so
        // the step text's min-content width would override the basis: a wide table,
        // a code block or a long unbreakable word would stretch the panel (and
        // resized the slide on every such step) instead of scrolling inside
        // its own box.
        minWidth: 0,
        minHeight: 0,
        // With the width capped, a long unbreakable word (a URL-like title, an
        // identifier) wraps at the edge rather than spilling out of the panel.
        // break-word, not anywhere: min-content sizes stay as they were, so a
        // wide table still scrolls instead of squeezing its columns.
        overflowWrap: 'break-word',
        background: 'var(--color-surface)',
        display: 'flex',
        flexDirection: 'column',
      }}>
        <div style={{
          padding: 'var(--space-4)',
          borderBottom: '1px solid var(--color-border)',
        }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 'var(--space-2)' }}>
            <div style={{
              flex: 1,
              // Lets a long unbreakable title wrap (overflowWrap on the
              // <aside>) instead of pushing the About button out of the panel.
              minWidth: 0,
              fontSize: 'var(--font-size-lg)',
              fontWeight: 700,
              color: 'var(--color-text)',
              letterSpacing: '-0.01em',
            }}>
              {tour?.title || t('tour.fallbackTitle')}
            </div>
            <SignOutButton />
            <button
              type="button"
              onClick={() => setShowAbout(true)}
              title="About this app"
              aria-label="About this app"
              style={{
                background: 'transparent',
                border: 'none',
                cursor: 'pointer',
                color: 'var(--color-text-faint)',
                padding: 4,
                lineHeight: 1,
                display: 'flex',
                alignItems: 'center',
                borderRadius: 4,
                flexShrink: 0,
              }}
            >
              <InfoIcon size={14} />
            </button>
          </div>
          {tour?.description && (
            <div style={{
              fontSize: 'var(--font-size-sm)',
              color: 'var(--color-text-muted)',
              marginTop: 'var(--space-2)',
              lineHeight: 1.55,
            }}>
              {tour.description}
            </div>
          )}
        </div>

        <div style={{
          padding: 'var(--space-3) var(--space-4)',
          borderBottom: '1px solid var(--color-border)',
          display: 'flex',
          gap: 'var(--space-2)',
          alignItems: 'center',
          flexWrap: 'wrap',
        }}>
          {/* Prev / Next / step picker share one line. When the panel is too
              narrow for all three, the picker shrinks (clipping its label)
              instead of wrapping onto a line of its own and pushing the step
              text down — hence the group and the two minWidth: 0. */}
          <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center', minWidth: 0 }}>
            <Button
              size="sm"
              variant="secondary"
              disabled={!steps.length || currentIdx === 0}
              onClick={() => setCurrentIdx(i => Math.max(0, i - 1))}
            >
              {t('tour.prev')}
            </Button>
            <Button
              size="sm"
              variant="primary"
              disabled={!steps.length || currentIdx === steps.length - 1}
              onClick={() => setCurrentIdx(i => Math.min(steps.length - 1, i + 1))}
            >
              {t('tour.next')}
            </Button>
            {/* Jump directly to any step. The counter is folded into the select so
                the "N / M" position stays visible without a separate element. */}
            <select
              aria-label={t('tour.jumpToStep')}
              disabled={!steps.length}
              value={steps.length ? currentIdx : ''}
              onChange={(e) => setCurrentIdx(Number(e.target.value))}
              style={{
                fontSize: 'var(--font-size-sm)',
                color: 'var(--color-text)',
                background: 'var(--color-bg)',
                border: '1px solid var(--color-border)',
                borderRadius: 'var(--radius-md, 6px)',
                padding: '4px 6px',
                minWidth: 0,
                maxWidth: 200,
                cursor: steps.length ? 'pointer' : 'default',
              }}
            >
              {!steps.length && <option value="">—</option>}
              {steps.map((s, i) => (
                <option key={s.id} value={i}>
                  {`${i + 1} / ${steps.length} · ${s.label || t('tour.stepFallback', { order: s.order })}`}
                </option>
              ))}
            </select>
          </div>
          <Button
            size="sm"
            variant="secondary"
            disabled={!steps.length}
            title={t('tour.resetViewTitle')}
            onClick={() => frameStep(currentIdx, 'smooth')}
          >
            {t('tour.resetView')}
          </Button>
          <label style={{
            marginLeft: 'auto',
            fontSize: 'var(--font-size-sm)',
            color: 'var(--color-text-muted)',
            display: 'flex',
            gap: 6,
            alignItems: 'center',
            cursor: 'pointer',
          }}>
            <input
              type="checkbox"
              checked={showAnnotations}
              onChange={(e) => setShowAnnotations(e.target.checked)}
              style={{ accentColor: 'var(--color-brand-600)' }}
            />
            {t('tour.annotations')}
          </label>
        </div>

        {/* When stacked, keep a readable floor for the step text so a long
            tour description scrolls the panel instead of collapsing this.
            The step text stops overscroll itself only side by side: stacked,
            the <aside> scrolls as a whole and does, and marking both would
            stop a swipe on the step text at its end instead of carrying on
            to the aside, leaving its header unreachable by swiping the text. */}
        <div className={stacked ? undefined : 'overscroll-panel'} style={{ flex: 1, minHeight: stacked ? 96 : 0, overflow: 'auto', padding: 'var(--space-4)' }}>
          {/* Only once this route's tour has loaded: before that (and after a
              failed load, which has its own banner) the empty list says
              nothing about the tour. */}
          {tour && !steps.length && (
            <div style={{
              color: 'var(--color-text-subtle)',
              fontSize: 'var(--font-size-sm)',
            }}>
              {t('tour.noSteps')}
            </div>
          )}
          {currentStep && (
            <>
              {currentStep.label && (
                <h3 style={{
                  marginTop: 0,
                  marginBottom: 'var(--space-3)',
                  fontSize: 'var(--font-size-xl)',
                  fontWeight: 600,
                  color: 'var(--color-text)',
                  letterSpacing: '-0.01em',
                }}>
                  {currentStep.label}
                </h3>
              )}
              <div className="jp-prose">
                <SafeMarkdown markdown={currentStep.description_md || ''} />
              </div>
            </>
          )}
        </div>
      </aside>

      {showAbout && <AboutDialog onClose={() => setShowAbout(false)} />}
    </div>
  )
}
