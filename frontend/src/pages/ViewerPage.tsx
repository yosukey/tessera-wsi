import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router'
import OpenSeadragon from 'openseadragon'
import { OSD_PREFIX_URL, osdMaxTilesPerFrame, attachHomeResetsRotation, attachNoFlipKey, attachOpenFailed, attachTwoFingerRotate, attachZoomClamp, loadTileSource } from '../lib/osd'
import { DEFAULT_VIEWER_SETTINGS, adminSaveSlideImageAdjust, bearerHeaders, getSlide, getToken, getViewerSettings, httpStatusOf, isAbortError, isManagerOrAboveFromToken, isRateLimitError, isStaffFromToken, isSziSlide, Slide, ViewerSettings } from '../lib/api'
import { logger } from '../lib/logger'
import { retryDelayMs } from '../lib/retryDelay'
import { useAuthGate } from '../lib/useAuthGate'
import { useI18n } from '../lib/i18n'
import SlideSwitcher from '../components/SlideSwitcher'
import AboutDialog from '../components/AboutDialog'
import SlideCredit, { ViewerInfoCorner } from '../components/SlideCredit'
import SlideInfoPanel from '../components/SlideInfoPanel'
import MouseCoordsOverlay from '../components/MouseCoordsOverlay'
import InfoIcon from '../components/icons/InfoIcon'
import FollowerJoinDialog from '../components/FollowerJoinDialog'
import FollowerEndedOverlay, { followerOverlayContent } from '../components/FollowerEndedOverlay'
import SyncPresenterLauncher from '../components/SyncPresenterLauncher'
import SyncIdleToast from '../components/SyncIdleToast'
import SyncPresenterSidebar from '../components/SyncPresenterSidebar'
import DebugToggle from '../components/DebugToggle'
import ImageAdjustControls from '../components/ImageAdjustControls'
import { DEFAULT_IMAGE_ADJUST, ImageAdjust, cssFilterString, imageAdjustFromSlide } from '../lib/imageAdjust'
import { useSyncPresenterSession } from '../hooks/useSyncPresenterSession'
import { useSyncFollower } from '../hooks/useSyncFollower'
import { useSlideDwell } from '../hooks/useSlideDwell'
import { useViewportHeight } from '../hooks/useViewportHeight'
import { NO_PINCH_ZOOM_CLASS, usePreventPinchZoom } from '../hooks/usePreventPinchZoom'
import { useNoOverscroll } from '../hooks/useNoOverscroll'
import { useViewerOverlays } from '../hooks/useViewerOverlays'
import { useDebugParam } from '../hooks/useDebugParam'
import { useMouseCoords } from '../hooks/useMouseCoords'

export default function ViewerPage() {
  const recheckAuth = useAuthGate()
  const { id } = useParams()
  // The URL segment is the slide's unguessable public_id token, not the
  // numeric PK. It keys the public fetch, the tile source, analytics, sync
  // matching and viewport restore. The numeric id (from the loaded slide) is
  // used only for authenticated admin calls (image-adjust save, sync create).
  const slidePublicId = id ?? ''
  const nav = useNavigate()
  // Records "user opened slide → user left slide" with a wall-clock dwell
  // time, subtracting background-tab time. The backend resolves the token to
  // the internal id and silently drops the row when analytics_mode='off' or
  // the viewer is anonymous, so the hook is safe to call unconditionally.
  useSlideDwell(slidePublicId || null)

  const { t } = useI18n()
  const appHeight = useViewportHeight()
  const [slide, setSlide] = useState<Slide | null>(null)
  // Initial state must include every numeric field the OSD-init effect
  // depends on, otherwise the field is `undefined` on first render and
  // changes to a number once getViewerSettings() resolves — which would
  // re-fire the effect, destroy the freshly-built viewer, and rebuild
  // it from scratch (visible flash + duplicate tile fetches).
  const [viewerSettings, setViewerSettings] = useState<ViewerSettings>(DEFAULT_VIEWER_SETTINGS)
  // Whether getViewerSettings() has resolved (or failed) at least once. The
  // OSD-init effect below reads several settings that are baked into the viewer
  // at construction (max_zoom_pixel_ratio, immediate_render, max_image_cache_
  // count). Those are also in its dependency array, so building the viewer with
  // the hardcoded initial defaults and then letting the real settings arrive
  // would destroy and rebuild the freshly built viewer (visible flash +
  // duplicate tile fetches, and for a live presenter an extra broadcast frame).
  // Gate the first build on the settings being known so the viewer is built
  // once, with the real values. Never reset to false — settings are global and
  // load once — so a later slide change doesn't re-gate.
  const [viewerSettingsLoaded, setViewerSettingsLoaded] = useState(false)
  const showZoomIndicator = slide?.show_zoom_indicator ?? viewerSettings.show_zoom_indicator
  const showScaleBar = slide?.show_scale_bar ?? viewerSettings.show_scale_bar
  const showMetadataPanel = slide?.show_metadata_panel ?? viewerSettings.show_metadata_panel
  const [searchParams] = useSearchParams()
  const [debugMode, toggleDebug] = useDebugParam()
  const fromAdmin = searchParams.get('from') === 'admin'
  const syncId = searchParams.get('sync') || ''
  const isFollower = syncId.length > 0

  const viewerDivRef = useRef<HTMLDivElement | null>(null)
  // Page-zoom gestures are cancelled across the whole page (see the root <div>
  // below), as in the tour viewer.
  const pageRef = useRef<HTMLDivElement | null>(null)
  usePreventPinchZoom(pageRef)
  // No rubber-band / stretch when a side panel is dragged past its end, nor
  // of the fixed-height page itself (see useNoOverscroll).
  useNoOverscroll()
  const osdRef = useRef<OpenSeadragon.Viewer | null>(null)
  // Viewport state captured by the previous OSD instance's cleanup so a
  // same-slide re-init (debug toggle / settings change) restores the user's
  // place. Keyed by the slide token so a slide-change re-init doesn't
  // accidentally pull the prior slide's viewport onto the new one.
  const savedViewportRef = useRef<{
    slidePublicId: string
    bounds: OpenSeadragon.Rect
    rotation: number
  } | null>(null)
  const [osdViewer, setOsdViewer] = useState<OpenSeadragon.Viewer | null>(null)
  const [showAbout, setShowAbout] = useState(false)
  // Bumped on every OSD (re)open so overlay effects (scale bar, zoom
  // indicator, digital-zoom badge) re-run against a viewer whose viewport
  // is fully initialised. Without this they race the async viewer-creation
  // IIFE: when slide metadata is cached, the overlay effects can run
  // before osdRef.current is assigned, bail on the null guard, and — since
  // their deps haven't changed — never re-attach. That manifests as a
  // frozen scale bar or a brief digital-zoom flash on first paint.
  const [osdGen, setOsdGen] = useState(0)
  // Image adjustment (brightness/contrast/saturation). Seeded from the slide's
  // saved preset on load (so dark scans are corrected from first paint) and
  // re-seeded on slide change; further tweaks are session-only and revert to
  // the preset on reload. A manager viewing via the dashboard can persist the
  // current value back as the slide's preset.
  const [imageAdjust, setImageAdjust] = useState<ImageAdjust>(DEFAULT_IMAGE_ADJUST)
  // Banner shown when slide / settings fetch fails. Without surfacing the
  // failure the page would look identical to a slow network and the user
  // would just see a stuck spinner-less viewer. Holds an i18n key.
  const [loadError, setLoadError] = useState<string | null>(null)

  const {
    followerStatus,
    followerSlideRef,
    followerRequireName,
    followerConnState,
    followerHasConnected,
    endedReason,
    endedNext,
    followerSlideUnavailable,
    presenterAbsent,
    presenterGraceRemaining,
    cursorOverlayRef,
    refitToLatestView,
    reprojectCursor,
    joinSession,
    cancelJoin,
    leaveSession,
    retryPeek,
  } = useSyncFollower(syncId, slidePublicId, osdRef)

  useEffect(() => {
    // A missing token can't resolve; redirect to the index instead of issuing
    // a doomed request and pinning a raw error banner.
    if (!slidePublicId) {
      nav('/', { replace: true })
      return
    }
    const ctrl = new AbortController()
    setLoadError(null)
    // Admin previews need to see drafts. The public /api/slides/{public_id}
    // endpoint 404s on unpublished slides for anonymous callers, but serves
    // them to a valid admin JWT (mirrors the /dzi/ draft-preview bypass), so
    // when the visitor arrived from the admin dashboard (?from=admin) and still
    // holds an admin token, forward it. Anyone else fetches unauthenticated.
    const adminToken = (fromAdmin && isStaffFromToken()) ? getToken() : null
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    let attempt = 0
    const fetchSlide = () => getSlide(slidePublicId, { signal: ctrl.signal, adminToken }).then(s => {
      // A retry after a 429 (below) clears the banner the refusal put up.
      setLoadError(null)
      setSlide(s)
      if (s) {
        // Apply the slide's saved adjustment preset as the starting point.
        setImageAdjust(imageAdjustFromSlide(s))
        const ref = s.catalog_id || `#${s.id}`
        document.title = s.title ? `${ref}: ${s.title}` : ref
      }
    }).catch((err) => {
      if (isAbortError(err)) return
      logger.error('failed to load slide', err)
      // The session may have expired while this page stayed open (following a
      // presenter from slide to slide): send the visitor to sign in, as on opening.
      if (httpStatusOf(err) === 401) recheckAuth()
      // Drop any previously-loaded slide so a failed navigation doesn't leave
      // the old slide's image, title, and metadata rendered underneath the
      // error banner (the URL and banner would say slide B while the viewer
      // still shows slide A). The OSD-init guard keys on slide.public_id, so
      // clearing it also tears the stale viewer down.
      setSlide(null)
      // Stored as an i18n key: err.message is an English fallback, an internal
      // code ("rate-limited") or even a raw nginx HTML body on a 502.
      setLoadError(isRateLimitError(err) ? 'common.rateLimited' : 'viewer.slideLoadFailed')
      // A 429 passes: try again rather than leave the page empty until the
      // next navigation. It matters most to live-sync followers, who all
      // fetch the new slide at the instant the presenter switches — a class
      // behind one NAT address is one client to the rate limiter, and a
      // follower has nothing to click to recover. The jitter spreads the
      // retries so they do not arrive together again.
      if (isRateLimitError(err)) {
        retryTimer = setTimeout(() => { retryTimer = null; fetchSlide() }, retryDelayMs(attempt++))
      }
    })
    fetchSlide()
    // Viewer settings are not fatal; log them but keep going with the
    // built-in defaults. Surface only as a console warning.
    getViewerSettings({ signal: ctrl.signal })
      .then((s) => { setViewerSettings(s); setViewerSettingsLoaded(true) })
      .catch((err) => {
        if (isAbortError(err)) return
        logger.warn('failed to load viewer settings', err)
        // Unblock OSD init on failure too so a settings outage doesn't leave
        // the viewer permanently unbuilt — it just uses the built-in defaults.
        setViewerSettingsLoaded(true)
      })
    return () => {
      ctrl.abort()
      if (retryTimer != null) clearTimeout(retryTimer)
    }
    // fromAdmin is read inside the body to decide whether to forward the admin
    // token (draft-preview bypass); without it in deps, toggling ?from=admin
    // (e.g. opening the public URL of a slide previously viewed via the admin
    // dashboard) wouldn't re-fetch through the right path and a previous
    // fetch's loadError banner would stay pinned over the page.
  }, [slidePublicId, fromAdmin])

  // Init OSD. Re-creates the viewer when debugMode toggles so the option is
  // applied cleanly to every TiledImage; viewport bounds/rotation are
  // preserved across the re-init so the user keeps their place. The
  // bounds capture lives in the cleanup (which runs while the viewer is
  // still alive). Capturing bounds at the entry of the re-fired effect
  // would fail: by then the cleanup has already destroyed
  // the viewer and getBounds() would throw — silently resetting the
  // view to default on every debug toggle.
  //
  // Wait for the slide to load: SZI tile sources require an async fetch of
  // the archive's central directory before the viewer can open. DZI could
  // open synchronously, but routing through the same code path keeps the
  // lifecycle uniform.
  const isSzi = isSziSlide(slide)
  useEffect(() => {
    if (!viewerDivRef.current) return
    if (!slide || slide.public_id !== slidePublicId) return
    // Wait for the real viewer settings before the first build so the viewer
    // isn't constructed with default max_zoom_pixel_ratio / immediate_render /
    // max_image_cache_count and then torn down + rebuilt when they resolve.
    if (!viewerSettingsLoaded) return
    let cancelled = false
    // Followers must never fight the presenter. Disable every interactive
    // gesture so panning / zooming stays exclusive to the remote view
    // frames. OpenSeadragon merges each gestureSettings* block into its own
    // defaults for that pointer type, so every gesture any type turns on by
    // default is listed (touch defaults to double-tap-and-drag zoom), and
    // gestureSettingsUnknown covers pointers that report no known type. OSD's
    // pointer tracker itself stays on (no mouseNavEnabled: false), so the
    // canvas keeps absorbing wheel / touch input instead of passing it to the
    // page.
    const noGestures = {
      clickToZoom: false,
      dblClickToZoom: false,
      dblClickDragToZoom: false,
      scrollToZoom: false,
      dragToPan: false,
      pinchToZoom: false,
      pinchRotate: false,
      flickEnabled: false,
    }
    const followerGestures = isFollower
      ? {
          gestureSettingsMouse: noGestures,
          gestureSettingsTouch: noGestures,
          gestureSettingsPen: noGestures,
          gestureSettingsUnknown: noGestures,
          // gestureSettings* covers pointer input only — OSD's keyboard
          // handler (arrow keys, +/-, w/a/s/d, r, 0, etc.) is gated by the
          // top-level keyboardNavEnabled flag, which defaults to true.
          // Without this, a follower whose canvas has focus can pan/zoom
          // with the keyboard and fight the presenter until the next view
          // frame snaps them back.
          keyboardNavEnabled: false,
          showZoomControl: false,
          showHomeControl: false,
          showFullPageControl: false,
        }
      : {
          gestureSettingsMouse: { clickToZoom: false },
          // Rotation is handled by attachTwoFingerRotate, not OSD's
          // distance-gated pinchRotate (see the helper for why).
        }

    // Admin draft previews need to authenticate the /dzi/ tile fetches so
    // the server-side is_published gate lets them through. ``fromAdmin``
    // alone is a UX hint anyone can set; only attach the header when the
    // user actually holds a non-expired admin JWT.
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
        setLoadError(
          result.reason === 'szi-plugin-missing'
            ? 'viewer.sziPluginMissing'
            : 'viewer.sziOpenFailed'
        )
        return
      }
      // Drop any stale OSD-induced loadError from a prior re-init so a
      // recovered SZI fetch doesn't leave the error banner pinned over a
      // healthy viewer. Slide-fetch failures aren't reachable here (the
      // outer effect bails when slide is null), so this is safe.
      setLoadError(null)
      const v = OpenSeadragon({
        element: viewerDivRef.current,
        prefixUrl: OSD_PREFIX_URL,
        tileSources: result.tileSource,
        showRotationControl: !isFollower,
        showNavigator: !isFollower,
        maxZoomPixelRatio: viewerSettings.max_zoom_pixel_ratio,
        // Which tiles OSD fetches first after a zoom — not what it draws: in
        // both modes the coarser tiles already in memory stay on screen,
        // stretched, until sharper ones arrive, and every level loads
        // centre-outwards. true (the app's default): tiles at the display
        // resolution come first and in-between levels are skipped, so a
        // large zoom jump or a zoom-out needs fewer requests and turns sharp
        // sooner; an area with no coarser tile in memory (zooming back out
        // after the tile cache evicted the overview) stays empty until its
        // tile arrives. false (OSD's default): coarse levels come first, so
        // the view is never empty, at the cost of more requests and a later
        // finish. Admin-tunable (Advanced rendering settings); measured in
        // docs/developer-guide.md "Viewer rendering tuning".
        immediateRender: viewerSettings.immediate_render,
        // Number of decoded tiles OSD keeps in memory (a count, not bytes;
        // admin-tunable, default 200). Raising it keeps low-zoom overview
        // tiles resident across deep panning; the cost is browser RAM.
        maxImageCacheCount: viewerSettings.max_image_cache_count,
        // More tile loads per frame on a high-DPI screen (see the helper).
        maxTilesPerFrame: osdMaxTilesPerFrame(),
        ...followerGestures,
        // Switch DZI tile loading from <img src> to XHR so the
        // ``Authorization`` header rides along with each tile request.
        // No-op for the SZI path (which has its own fetch with the
        // headers wired through ``loadTileSource``). When no admin token
        // is in scope, leave OSD on its default <img> loader so nginx
        // can keep serving published tiles from its public cache.
        ...(ajaxHeaders ? { loadTilesWithAjax: true, ajaxHeaders } : {}),
        debugMode,
      })
      osdRef.current = v
      setOsdViewer(v)
      // A DZI descriptor that fails to load (404 after an unpublish, a 5xx)
      // gets the same translated banner as a failed slide fetch.
      const detachOpenFailed = attachOpenFailed(v, () => setLoadError('viewer.slideLoadFailed'))
      // Followers must NOT clamp zoom to their own getMaxZoom(). The presenter
      // frames a view via device-independent viewport bounds, but getMaxZoom()
      // is container-size dependent (imageWidth * maxZoomPixelRatio /
      // containerWidth), so a follower on a larger/denser screen than the
      // presenter would have fitBounds() silently clamped below the presenter's
      // transmitted zoom — the follower ends up zoomed out and desynced. The
      // clamp only exists to stop a *user* over-zooming with their own
      // gestures, and a follower has none: every pointer gesture of every
      // pointer type, the keyboard and the zoom buttons are off above, so
      // there is nothing for it to protect. Skip it (like the rotation
      // helpers below) so fitBounds always reproduces the presenter's exact
      // magnification.
      const detachZoomClamp = isFollower ? () => {} : attachZoomClamp(v)
      // Followers never interact, so two-finger rotation only applies to
      // the live (non-follower) viewer.
      const detachRotate = isFollower ? () => {} : attachTwoFingerRotate(v)
      // Followers have no home control (showHomeControl is off above); the
      // live viewer's Go Home button should also bring rotation back to 0.
      const detachHomeReset = isFollower ? () => {} : attachHomeResetsRotation(v)
      // No mirror-image slide from a stray ``f`` key, for anyone: a presenter
      // would also be left on a view the followers do not share.
      const detachNoFlip = attachNoFlipKey(v)
      v.addOnceHandler('open', () => {
        try {
          // Follower: prefer the remote view if it is already available.
          if (!(isFollower && refitToLatestView())) {
            // Otherwise fall back to the viewport the previous OSD
            // instance for THIS slide left in savedViewportRef. The
            // token check guards against restoring slide A's view onto
            // slide B (different image coordinate spaces).
            const saved = savedViewportRef.current
            if (saved && saved.slidePublicId === slidePublicId) {
              v.viewport.setRotation(saved.rotation)
              v.viewport.fitBounds(saved.bounds, true)
            }
          }
        } catch {}
        savedViewportRef.current = null
        setOsdGen(g => g + 1)
      })
      // Keep the cursor overlay glued to its image-coordinates anchor as the
      // viewport pans/zooms (including during follower-side animation).
      const onUpdate = () => reprojectCursor()
      v.addHandler('update-viewport', onUpdate)
      // Follower: put the presenter's field back after the viewer resizes
      // (window resize, device rotation), which OSD answers by keeping the old
      // centre and scale. Its 'resize' event fires before OSD re-centres and
      // re-zooms, so the refit waits for the 'viewport-change' that the same
      // update raises before drawing.
      let refitPending = false
      const onResize = () => { refitPending = true }
      const onResized = () => {
        if (!refitPending) return
        refitPending = false
        refitToLatestView()
      }
      if (isFollower) {
        v.addHandler('resize', onResize)
        v.addHandler('viewport-change', onResized)
      }

      cleanup = () => {
        // Capture the viewport BEFORE destroying so the next effect run
        // (debug toggle / settings change) can restore it. Tagged with the
        // slide token so a slide-change cleanup can't bleed into the new
        // slide's viewport.
        try {
          savedViewportRef.current = {
            slidePublicId,
            bounds: v.viewport.getBounds(),
            rotation: v.viewport.getRotation(),
          }
        } catch {
          savedViewportRef.current = null
        }
        detachOpenFailed()
        detachZoomClamp()
        detachRotate()
        detachHomeReset()
        detachNoFlip()
        v.removeHandler('update-viewport', onUpdate)
        v.removeHandler('resize', onResize)
        v.removeHandler('viewport-change', onResized)
        v.destroy()
        if (osdRef.current === v) osdRef.current = null
        setOsdViewer((prev) => (prev === v ? null : prev))
      }
    })()

    return () => {
      cancelled = true
      if (cleanup) cleanup()
    }
  }, [slidePublicId, debugMode, isFollower, isSzi, slide?.id, slide?.tile_version, viewerSettingsLoaded, viewerSettings.max_zoom_pixel_ratio, viewerSettings.immediate_render, viewerSettings.max_image_cache_count, fromAdmin])

  const { scaleBarDivRef, zoomIndicatorDivRef, digitalZoomBadgeDivRef } = useViewerOverlays({
    viewerRef: osdRef, slide, osdGen, showScaleBar, showZoomIndicator,
  })
  // The coords overlay only renders in debug mode, so skip the per-move
  // setState (a re-render of this whole page per mouse event) otherwise.
  const mouseCoords = useMouseCoords(osdRef, debugMode, osdGen)

  // Suppress most of the admin-only chrome while in follower mode so the
  // visitor sees a clean, distraction-free image.
  const showAdminChrome = fromAdmin && !isFollower
  // Live sync is admin-only on the server. Only surface the launch button
  // when the visitor actually holds a valid admin JWT — otherwise clicks
  // would 401 silently from /api/admin/sync/sessions and the button looks
  // broken. DebugToggle is intentionally still gated only by ?from=admin
  // (showAdminChrome) since it is a pure UX affordance with no server state;
  // SlideSwitcher sits in the same admin chrome but additionally renders only
  // while a live-sync session is active (gated on `liveSession` below).
  //
  // Also hide the launcher on unpublished (draft/private) slides: the server
  // refuses to start live sync on them (followers couldn't fetch the tiles),
  // so surfacing the button here would only produce a dead-end 403. Admin
  // previews carry is_published via the admin-token draft bypass on getSlide.
  const showSyncPresenter =
    showAdminChrome && isStaffFromToken() && slide?.is_published === true

  // Presenter session state owned at this layer to allow swapping layouts
  // (full-bleed when idle, flex-row + sidebar when live). Live sync is keyed
  // by the numeric slide id (the create endpoint + view frames use the PK; the
  // server maps it to the public_id for the follower share URL).
  // ``showSyncPresenter`` only gates starting (and the resume probe). A
  // session that is already live stays on screen whatever slide the presenter
  // moves to: on an unpublished one the server keeps the room (followers stay
  // on the previous slide; the presenter is told), and the presenter still needs Stop,
  // the sidebar, the slide switcher and the unload warning.
  const syncSession = useSyncPresenterSession(slide?.id ?? 0, osdViewer, showSyncPresenter)
  const liveSession = syncSession.session

  // Warn the presenter before they accidentally close/navigate away while live.
  const isLive = !!liveSession
  useEffect(() => {
    if (!isLive) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [isLive])

  // The flex reflow on sidebar mount/unmount/collapse shrinks OSD's
  // container synchronously, but OSD's ResizeObserver-driven catch-up
  // is one rAF tick late: the browser paints one frame with the old
  // (wider) <canvas> pixel buffer CSS-scaled into the new (narrower)
  // box, which reads as a brief horizontal squish on the slide. Run
  // the resize + redraw explicitly in a layout effect so the new pixel
  // buffer lands in the same paint as the new layout.
  const resyncOsdToContainer = useCallback(() => {
    const v = osdRef.current
    const el = v?.element
    if (!v || !el) return
    try {
      v.viewport.resize(new OpenSeadragon.Point(el.clientWidth, el.clientHeight), false)
      v.world.draw()
    } catch { /* swallow — viewer torn down mid-flight */ }
  }, [])
  useLayoutEffect(() => {
    resyncOsdToContainer()
  }, [!!liveSession, resyncOsdToContainer])

  return (
    <div ref={pageRef} className={NO_PINCH_ZOOM_CLASS} style={{
      height: appHeight,
      display: 'flex',
      position: 'relative',
      // Scrolling only, no page pinch-zoom: a tablet user pinches the slide
      // all the time, and a pinch starting just off it lands on an overlay
      // over the slide (info panel, credit, scale bar, …) or the presenter
      // sidebar and would zoom the whole page. Browsers that honour touch-action
      // stop that from this alone, with NO_PINCH_ZOOM_CLASS repeating it on
      // every descendant: iOS Safari starts again from auto inside a scrolling
      // panel. The rest (trackpad pinch) is cancelled by usePreventPinchZoom.
      // The slide's own pinch is untouched: OpenSeadragon sets touch-action:
      // none on its element (the class leaves it alone) and reads the gesture
      // from pointer events there.
      touchAction: 'pan-x pan-y',
    }}>
      {/* Left column: OSD canvas + all overlays */}
      <div style={{ flex: 1, minWidth: 0, height: '100%', position: 'relative' }}>
        <ViewerInfoCorner>
          {showMetadataPanel && <SlideInfoPanel slide={slide} settings={viewerSettings} />}
          <SlideCredit key={slide?.public_id} credit={slide} />
          <AboutInfoButton onClick={() => setShowAbout(true)} />
        </ViewerInfoCorner>

        {showAdminChrome && (
          <>
            <DebugToggle enabled={debugMode} onToggle={toggleDebug} />
            {liveSession && <SlideSwitcher />}
            {showSyncPresenter && !liveSession && (
              <SyncPresenterLauncher
                onStart={syncSession.start}
                onResume={syncSession.resume}
                resumable={!!syncSession.resumable}
                resumeTakesOver={!!syncSession.resumable?.presenter_connected}
                error={syncSession.startError}
              />
            )}
          </>
        )}

        <div
          ref={viewerDivRef}
          className="osd-with-adjust"
          style={{
            width: '100%',
            height: '100%',
            ['--osd-filter' as any]: cssFilterString(imageAdjust),
          }}
        />
        {loadError && (
          <div style={{
            position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
            zIndex: 40, background: '#fef2f2', color: '#991b1b',
            border: '1px solid #fecaca', padding: '8px 14px', borderRadius: 6,
            fontSize: 13, fontWeight: 500, maxWidth: '90%',
          }}>
            {t(loadError)}
          </div>
        )}
        {/* Presenter-only inactivity toast (warning countdown / auto-ended).
            idleNotice is only ever set for the presenter driving this session. */}
        <SyncIdleToast
          notice={syncSession.idleNotice}
          onDismiss={syncSession.dismissIdleNotice}
        />
        {!isFollower && (
          <ImageAdjustControls
            value={imageAdjust}
            onChange={setImageAdjust}
            baseline={imageAdjustFromSlide(slide)}
            canSavePreset={showAdminChrome && isManagerOrAboveFromToken()}
            onSavePreset={async (v) => {
              // Admin write keyed by the numeric id from the loaded slide.
              if (!slide) return
              const slideId = slide.id
              const res = await adminSaveSlideImageAdjust(slideId, v)
              // Fold the persisted preset back into the in-memory slide so the
              // baseline (and the reset target / "modified" dot) updates
              // without a reload — but only into the slide it was saved for:
              // the presenter may have switched slides while it was in flight.
              setSlide(prev => prev && prev.id === slideId ? {
                ...prev,
                image_adjust_brightness: res.image_adjust_brightness,
                image_adjust_contrast: res.image_adjust_contrast,
                image_adjust_saturation: res.image_adjust_saturation,
              } : prev)
            }}
          />
        )}
        {showScaleBar && <div ref={scaleBarDivRef as any}></div>}
        {showZoomIndicator && <div ref={zoomIndicatorDivRef as any}></div>}
        <div ref={digitalZoomBadgeDivRef as any}></div>
        {debugMode && <MouseCoordsOverlay coords={mouseCoords} />}

        {/* Follower-only overlays */}
        {isFollower && (
          <div
            ref={cursorOverlayRef}
            aria-hidden
            style={cursorOverlayStyle}
          />
        )}

        {isFollower && followerStatus === 'join' && (
          <FollowerJoinDialog
            slideRef={followerSlideRef}
            requireName={followerRequireName}
            onJoin={joinSession}
            onCancel={cancelJoin}
          />
        )}

        {isFollower && (followerStatus === 'notfound' || followerStatus === 'ended' || followerStatus === 'error' || followerStatus === 'full' || followerStatus === 'unauthorized' || followerStatus === 'forbidden') && (
          <FollowerEndedOverlay
            {...followerOverlayContent(followerStatus, endedReason, endedNext, t, retryPeek)}
          />
        )}

        {isFollower && followerStatus === 'connected' && (
          <FollowerLeaveButton onClick={leaveSession} label={t('sync.leaveSession') as string} />
        )}

        {isFollower && followerStatus === 'connected' && followerConnState !== 'open' && (
          <FollowerStatusBanner
            message={
              followerConnState === 'connecting'
                ? followerHasConnected
                  ? (t('sync.sessionUnstable') as string)
                  : (t('sync.connecting') as string)
                : (t('sync.disconnected') as string)
            }
          />
        )}

        {/* Amber like the absence notice, below the connection banner; the
            absence notice (which ends the session) wins when both apply. */}
        {isFollower && followerStatus === 'connected' && followerSlideUnavailable && !presenterAbsent && (
          <PresenterAbsentBanner message={t('sync.followerSlideUnavailable') as string} />
        )}

        {isFollower && followerStatus === 'connected' && presenterAbsent && (
          <PresenterAbsentBanner
            message={t('sync.presenterLeft', { seconds: presenterGraceRemaining ?? 0 }) as string}
          />
        )}
      </div>

      {showAbout && <AboutDialog onClose={() => setShowAbout(false)} />}

      {/* Right column: presenter session dock (only while live) */}
      {liveSession && (
        <SyncPresenterSidebar
          session={liveSession}
          audience={syncSession.audience}
          connState={syncSession.connState}
          onStop={syncSession.stop}
          onCollapseChange={resyncOsdToContainer}
        />
      )}
    </div>
  )
}

const cursorOverlayStyle: React.CSSProperties = {
  position: 'absolute',
  width: 24,
  height: 24,
  borderRadius: '50%',
  background: 'rgba(220, 38, 38, 0.45)',
  boxShadow: '0 0 14px rgba(220, 38, 38, 0.65)',
  pointerEvents: 'none',
  display: 'none',
  transform: 'translate3d(-9999px, -9999px, 0)',
  willChange: 'transform',
  zIndex: 20,
  top: 0,
  left: 0,
}

function FollowerLeaveButton({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        position: 'absolute', top: 12, right: 12, zIndex: 30,
        background: 'rgba(0, 0, 0, 0.65)',
        color: '#fff',
        border: '1px solid rgba(255, 255, 255, 0.25)',
        borderRadius: 6,
        padding: '6px 14px',
        fontSize: 12,
        fontWeight: 500,
        cursor: 'pointer',
        letterSpacing: '0.02em',
      }}
    >
      {label}
    </button>
  )
}

function FollowerStatusBanner({ message }: { message: string }) {
  return (
    <div style={{
      position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
      zIndex: 30, background: 'rgba(0, 0, 0, 0.78)', color: '#fff',
      padding: '6px 14px', borderRadius: 999, fontSize: 12, fontWeight: 500,
      letterSpacing: '0.02em',
    }}>
      {message}
    </div>
  )
}

// Warning banner shown to followers while the presenter is mid-absence. Amber
// (vs the neutral connection banner) and sits just below it so the two can
// stack if a follower's own connection is also flapping. Carries the live
// countdown to the grace deadline; when it runs out the server's `end` frame
// flips the follower into the terminal "session ended" overlay.
function PresenterAbsentBanner({ message }: { message: string }) {
  return (
    <div style={{
      position: 'absolute', top: 48, left: '50%', transform: 'translateX(-50%)',
      zIndex: 30, background: 'rgba(180, 83, 9, 0.92)', color: '#fff',
      padding: '6px 14px', borderRadius: 999, fontSize: 12, fontWeight: 600,
      letterSpacing: '0.02em', maxWidth: '90vw', textAlign: 'center',
    }}>
      {message}
    </div>
  )
}

// The last item of the bottom-right info column (below the metadata card and
// the image credit), so it never lands on the bottom-left tools or the card.
function AboutInfoButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      // The About modal and this trigger's tooltip / aria-label are
      // intentionally English-only (see AdminDashboard's About trigger):
      // version, license, and attribution strings are not translated.
      title="About this app"
      aria-label="About this app"
      style={{
        background: 'rgba(255, 255, 255, 0.82)',
        border: 'none',
        borderRadius: 5,
        cursor: 'pointer',
        color: 'rgba(0, 0, 0, 0.85)',
        padding: '4px 8px',
        lineHeight: 1,
        display: 'flex',
        alignItems: 'center',
        pointerEvents: 'auto',
      }}
    >
      <InfoIcon size={14} />
    </button>
  )
}
