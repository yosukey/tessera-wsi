// OpenSeadragon helpers shared by ViewerPage, TourViewerPage, and the
// admin tour editor. Each page wires its own useEffect lifecycle (the
// post-init handlers diverge widely — follower mode disables every
// gesture, the tour editor adds shape-drawing handlers, etc.) so the
// lifecycle stays inline; only the pieces the pages share verbatim
// are pulled into helpers.
//
// Pulling this out also provides a single place to bump the OSD prefix
// URL or swap the SZI tile-source factory if the upstream contract ever
// changes.
import OpenSeadragon from 'openseadragon'
// szi-tile-source ships no .d.ts; suppress the implicit-any.
// @ts-ignore — runtime-only ESM import from the upstream GitHub package.
import { enableSziTileSource } from 'szi-tile-source'
import { logger } from './logger'
import type { TransitionMode } from './api'
import { dziDescriptorUrl, sziArchiveUrl } from './tileUrl'

// Registers OpenSeadragon.SziTileSource, which loadTileSource below needs.
// Done here, at import, rather than in main.tsx: every viewer reaches OSD
// through this module, so the registration always precedes its first use,
// and OpenSeadragon stays in the lazily loaded viewer chunks instead of the
// entry chunk every page (index, login, admin) downloads.
enableSziTileSource(OpenSeadragon)

// OSD UI sprites (zoom in/out, home, full page, rotate left/right), served
// from this app's own origin out of frontend/public/osd-images/.
//
// The set is the flat "openseadragon-svg-icons" look, not the beveled sprites
// that ship inside the openseadragon package — this is the viewer's deliberate
// visual identity, so do NOT refresh these from
// node_modules/openseadragon/build/openseadragon/images/ when bumping that
// dependency. The 24 PNGs (6 controls x rest/hover/pressed/grouphover, 40x40)
// are vendored verbatim from
//
//   https://github.com/Benomrans/openseadragon-icons
//   @ 6f0d2d03d12f5e4c1ae376f0d2917321981cafcf, images/
//
// See THIRD_PARTY_LICENSES.md for the provenance chain and license.
//
// Self-hosted rather than CDN-loaded: the primary deployment shape is an
// isolated LAN with no outbound internet, and it lets the CSP `img-src` drop
// the external origin.
export const OSD_PREFIX_URL = '/osd-images/'

// New tile loads OpenSeadragon may start per animation frame (its option
// maxTilesPerFrame, default 1). At 1, loading is capped by the frame rate —
// 60 tiles/s at 60 fps, less while a busy device draws — however fast the
// server answers, and one screen takes tiles in proportion to the pixel ratio
// squared: roughly 50-160 on a 1080p monitor, 90-280 on an iPad, 120-380 on
// a Retina laptop at deep zoom. So the limit follows that: 1 at a pixel ratio
// of 1 (a desktop screen fills within a zoom animation anyway, and more per
// frame would mostly fetch tiles of the levels the animation passes through),
// 2 at 1.25-1.5, 4 at 2 and above. Each frame still starts the best tiles
// first. Measured in docs/developer-guide.md "Viewer rendering tuning".
export function osdMaxTilesPerFrame(pixelRatio: number = window.devicePixelRatio || 1): number {
  return Math.max(1, Math.min(4, Math.round(pixelRatio * pixelRatio)))
}

/**
 * Resolve the OSD `tileSources` value for a slide. SZI archives need an
 * async fetch of their central directory before the viewer can open;
 * regular DZI is just a URL.
 *
 * On the SZI path, ``ajaxHeaders`` (when provided) ride through to the
 * underlying fetch via szi-tile-source's ``fetchOptions`` so admin
 * draft previews can authenticate the same way the DZI path does.
 *
 * ``tileVersion`` is the slide's ``tile_version`` from the API: it becomes the
 * ``?v=`` of the descriptor / archive URL (and, via OpenSeadragon, of every
 * tile URL), so a replaced WSI is fetched under new URLs. See lib/tileUrl.
 * Callers must re-open the viewer when it changes.
 *
 * Returns a discriminated result so the caller can distinguish the two
 * SZI-only failure modes (plugin missing vs. fetch/parse failure) from
 * the success path.
 */
// `tileSource` is typed as `any` to match the (broad) shape OSD's
// `tileSources` constructor option accepts: a URL string, an OSD-managed
// TiledImage instance (returned by createSziTileSource), or an inline
// DZI/IIIF descriptor object. The OpenSeadragon type union is too narrow
// to express the SziTileSource instance, so the helper returns `any` and
// lets the OpenSeadragon constructor validate at runtime.
export type TileSourceResult =
  | { ok: true; tileSource: any }
  | { ok: false; reason: 'szi-plugin-missing' | 'szi-fetch-failed'; error?: unknown }

export async function loadTileSource(
  slidePublicId: string,
  isSzi: boolean,
  ajaxHeaders?: Record<string, string>,
  tileVersion?: number | null,
): Promise<TileSourceResult> {
  // Tiles are addressed by the slide's public_id token: OpenSeadragon derives
  // every `_files/{level}/{col}_{row}.jpeg` URL from this `.dzi` base — query
  // string included (DziTileSource's `queryParams`) — so the token and the
  // `?v=` generation propagate to the whole pyramid. The numeric PK is never
  // in a tile URL.
  if (!isSzi) return { ok: true, tileSource: dziDescriptorUrl(slidePublicId, tileVersion) }
  const Sz = (OpenSeadragon as unknown as {
    SziTileSource?: {
      createSziTileSource(
        url: string,
        fetchOptions?: { headers?: Record<string, string>; mode?: RequestMode; credentials?: RequestCredentials },
      ): Promise<unknown>
    }
  }).SziTileSource
  if (!Sz) {
    logger.error('SZI tile source plugin is not loaded')
    return { ok: false, reason: 'szi-plugin-missing' }
  }
  try {
    const fetchOptions = ajaxHeaders ? { headers: ajaxHeaders } : undefined
    // Every Range read goes to this exact URL, so a replace between two reads
    // turns into the server's 404 for the stale `v` rather than bytes of the
    // new archive spliced into the old central directory.
    const tileSource = await Sz.createSziTileSource(sziArchiveUrl(slidePublicId, tileVersion), fetchOptions)
    return { ok: true, tileSource }
  } catch (err) {
    logger.error('failed to load SZI tile source', err)
    return { ok: false, reason: 'szi-fetch-failed', error: err }
  }
}

/**
 * Report a failed open (e.g. the DZI descriptor 404s or the server is down)
 * through ``onFail`` so the page can show its own, translated error.
 *
 * OSD answers ``open-failed`` by writing its raw English message into the
 * viewer ("Unable to open [object Object]: HTTP 404 attempting to load
 * TileSource: /dzi/<token>.dzi?v=3"), internal URL included. Its handler is
 * registered in the Viewer constructor, so it has already run when this one
 * does; take that message down again.
 *
 * Returns a detacher.
 */
export function attachOpenFailed(viewer: OpenSeadragon.Viewer, onFail: () => void): () => void {
  const handler = () => {
    const v = viewer as unknown as { _hideMessage?: () => void }
    try { v._hideMessage?.() } catch { /* nothing shown */ }
    onFail()
  }
  viewer.addHandler('open-failed', handler)
  return () => viewer.removeHandler('open-failed', handler)
}

/**
 * Pinch zoom on mobile and the wheel-zoom on desktop can both overshoot
 * `maxZoomPixelRatio`. Clamp explicitly on every zoom event so the
 * constraint always holds, regardless of input device.
 *
 * Returns a detacher.
 */
export function attachZoomClamp(viewer: OpenSeadragon.Viewer): () => void {
  // The OSD ``zoom`` event carries the gesture's own reference point and
  // whether it zoomed immediately. Both are reused when correcting.
  const handler = (ev: { refPoint?: OpenSeadragon.Point | null; immediately?: boolean }) => {
    const vp = viewer.viewport
    if (!vp) return
    const max = vp.getMaxZoom()
    // Re-clamp the *target* zoom (``getZoom(false)``) whenever a zoom aims past
    // the limit. Two things make this hold across input devices:
    //
    // 1. Mirror the triggering gesture's immediacy. Touch pinch zooms the
    //    viewport *immediately* on every move (OSD's onCanvasPinch calls
    //    ``zoomBy(factor, centre, immediately=true)`` each frame and only
    //    *animates* its own constraint correction). An animated clamp can
    //    never catch a fast pinch, so the slide visibly sits past native until
    //    the fingers lift. Clamping with the same immediacy pins it to the
    //    limit frame-by-frame. Desktop wheel zoom is animated, so it keeps
    //    gliding smoothly to the edge.
    //
    // 2. Pivot about the gesture's own reference point — the pinch centre /
    //    cursor OSD just zoomed around — not the viewport centre. A centre
    //    pivot yanks the image sideways on every correction because the
    //    gesture anchor is offset from it. ``vp.zoomPoint`` is the same value
    //    OSD stashes for its constraint pass; fall back to it, then centre.
    //
    // ``getZoom(false) > max`` self-terminates: the corrective ``zoomTo`` sets
    // the target to ``max`` and re-fires ``zoom``, which then no-ops.
    if (vp.getZoom(false) > max) {
      const pivot =
        ev?.refPoint ??
        (vp as unknown as { zoomPoint?: OpenSeadragon.Point | null }).zoomPoint ??
        vp.getCenter(false)
      vp.zoomTo(max, pivot, ev?.immediately === true)
    }
  }
  viewer.addHandler('zoom', handler)
  return () => viewer.removeHandler('zoom', handler)
}

/**
 * Make OSD's "Go Home" button a full reset.
 *
 * ``Viewport.goHome`` only refits pan + zoom; it leaves the current rotation
 * untouched. The standard home control therefore can't bring a twisted slide
 * back upright. ``goHome`` raises a ``home`` event before it refits, so listen
 * for it and zero the rotation alongside, animating (or snapping) in step with
 * the refit via the event's ``immediately`` flag.
 *
 * Returns a detacher.
 */
export function attachHomeResetsRotation(viewer: OpenSeadragon.Viewer): () => void {
  const handler = (ev: OpenSeadragon.HomeEvent) => {
    const vp = viewer.viewport
    if (!vp) return
    if (vp.getRotation(true) !== 0) {
      vp.setRotation(0, ev.immediately)
    }
  }
  viewer.addHandler('home', handler)
  return () => viewer.removeHandler('home', handler)
}

/**
 * Turn off OSD's ``f`` key, which mirrors the slide horizontally.
 *
 * Its keyboard navigation flips the view on ``f`` / ``F`` (the viewer's flip
 * button is off, so the key is the only way). A mirrored histology slide is
 * never what a viewer wants, it is easy to hit by accident once the canvas has
 * focus, and nothing in the UI tells you how to undo it; in a live-sync
 * session it would also leave the presenter on a mirror image the followers
 * do not share. The rest of the keyboard navigation is untouched: OSD raises
 * ``canvas-key`` before acting on a key, and ``preventDefaultAction`` skips
 * its action for that key alone.
 *
 * Returns a detacher.
 */
export function attachNoFlipKey(viewer: OpenSeadragon.Viewer): () => void {
  const handler = (ev: OpenSeadragon.CanvasKeyEvent) => {
    // OSD's own test for its flip key.
    if ((ev.originalEvent as KeyboardEvent).keyCode === 70) ev.preventDefaultAction = true
  }
  viewer.addHandler('canvas-key', handler)
  return () => viewer.removeHandler('canvas-key', handler)
}

/**
 * Two-finger rotation for touch.
 *
 * OSD's built-in ``pinchRotate`` rides on the pinch handler, which only
 * fires when the distance between the two fingers changes (see
 * onCanvasPinch's ``delta !== currentPinchDist`` gate in the OSD source).
 * A pure two-finger twist keeps that distance roughly constant, so the
 * pinch handler — and therefore the rotation — barely fires; zoom works
 * because it inherently changes the finger distance. Drive rotation off
 * the *angle* between the two active touch pointers instead, decoupled
 * from distance, so a twist with no zoom still rotates.
 *
 * Callers must leave OSD's ``pinchRotate`` disabled, otherwise a combined
 * zoom+twist would rotate twice (once here, once in OSD's pinch handler).
 *
 * Returns a detacher.
 */
export function attachTwoFingerRotate(viewer: OpenSeadragon.Viewer): () => void {
  // The previous frame's angle, paired with the identity of the exact two
  // fingers it was measured from. Keying the baseline to the pair (not just
  // "any two touches") is half of what stops the 180° snap — see below.
  let lastAngle: number | null = null
  let lastPairKey: string | null = null

  // A real two-finger twist advances by at most a couple of degrees between
  // consecutive touch-move events (touch input samples at ~60-120Hz). Any
  // larger single-frame jump is a misdetection — a dropped or relabelled
  // pointer — never an actual gesture, so it's rejected instead of applied.
  // This is the over-reaction guard: it caps how far one stray frame can spin
  // the slide.
  const MAX_STEP_RAD = Math.PI / 4 // 45°

  const tracker = new OpenSeadragon.MouseTracker({
    element: viewer.element,
    moveHandler: (ev: { position: OpenSeadragon.Point }) => {
      const vp = viewer.viewport
      if (!vp) return
      const list = tracker.getActivePointersListByType('touch')
      if (list.getLength() !== 2) {
        lastAngle = null
        lastPairKey = null
        return
      }
      const p0 = list.getByIndex(0)
      const p1 = list.getByIndex(1)
      if (!p0 || !p1) {
        lastAngle = null
        lastPairKey = null
        return
      }
      // Order the two pointers by their stable id so the vector whose
      // angle is taken always points the same way, regardless of the (arbitrary)
      // order OSD stores them in this frame. Without this the list can swap
      // index 0<->1 between moves — flipping the a->b vector by exactly 180°.
      // The ±π wrap below then reads that flip as a real half-turn and snaps
      // the slide around by a half-turn.
      const [a, b] = p0.id <= p1.id ? [p0, p1] : [p1, p0]
      const pairKey = `${a.id}:${b.id}`
      const angle = Math.atan2(
        b.currentPos.y - a.currentPos.y,
        b.currentPos.x - a.currentPos.x,
      )
      // Only rotate when this frame is continuous with the last one: the SAME
      // finger pair, and a baseline already established. When the pair changes
      // (a finger lifts and another lands) the baseline restarts rather than
      // rotating across that discontinuity.
      if (lastAngle !== null && lastPairKey === pairKey) {
        let delta = angle - lastAngle
        // Normalise across the ±π wrap so a twist past the seam doesn't
        // snap the image a full turn the other way.
        if (delta > Math.PI) delta -= 2 * Math.PI
        else if (delta < -Math.PI) delta += 2 * Math.PI
        // Apply only plausible per-frame steps; reject residual glitches.
        // Resyncing the baseline (below) after a rejected frame means the
        // gesture picks straight back up without a jump.
        if (delta !== 0 && Math.abs(delta) <= MAX_STEP_RAD) {
          // Pivot about the midpoint of the two fingers (ev.position is
          // already element-relative) so the image turns under the grip.
          const pivot = vp.pointFromPixel(ev.position, true)
          vp.rotateTo(vp.getRotation(true) + (delta * 180) / Math.PI, pivot, true)
        }
      }
      lastAngle = angle
      lastPairKey = pairKey
    },
  })
  return () => tracker.destroy()
}

/**
 * Track the cursor's current image-coordinate position via OSD's
 * MouseTracker. Used by the debug coord overlay on every page.
 *
 * Returns a detacher.
 */
export function attachMouseCoordsTracker(
  viewer: OpenSeadragon.Viewer,
  onMove: (coords: { x: number; y: number } | null) => void,
): () => void {
  const tracker = new OpenSeadragon.MouseTracker({
    element: viewer.element,
    moveHandler: (ev: { position: OpenSeadragon.Point }) => {
      if (!viewer.viewport) return
      const vpPoint = viewer.viewport.pointFromPixel(ev.position)
      const imgPoint = viewer.viewport.viewportToImageCoordinates(vpPoint)
      onMove({ x: imgPoint.x, y: imgPoint.y })
    },
    leaveHandler: () => onMove(null),
  })
  return () => tracker.destroy()
}

// Transition modes ('none' | 'smooth' | 'smooth_zoom') are defined in the API
// layer (lib/api/tours) since they're part of the step's stored shape; this
// module just consumes the type to animate accordingly.

// Seconds for a tour step's animated transition. OSD's built-in default
// (`animationTime` ≈ 1.2s) snaps between fields too quickly to read as a
// visual effect, so guided tours move at a deliberately slower pace.
export const TOUR_TRANSITION_ANIMATION_TIME = 3.0

// How much wider than the union of the two fields the 'smooth_zoom' pull-back
// frames, so neither field sits flush against the viewport edge.
const OVERVIEW_PADDING = 1.3
// Share of the total glide spent pulling back vs. pushing in.
const PULL_OUT_FRACTION = 0.5

// An OSD viewport spring with its cached original animationTime. Stashing the
// baseline on the spring (rather than reading the live value) keeps re-entrant
// transitions from compounding the slowdown.
type SlowSpring = { animationTime: number; _tourBaseAnimationTime?: number }

function viewportSprings(viewer: OpenSeadragon.Viewer): SlowSpring[] {
  const vp = viewer.viewport as unknown as {
    centerSpringX?: SlowSpring
    centerSpringY?: SlowSpring
    zoomSpring?: SlowSpring
    // Rotation spring's property name has shifted across OSD versions; keep
    // both candidates and let the falsy filter drop whichever is absent.
    rotationSpring?: SlowSpring
    degreesSpring?: SlowSpring
  }
  return [
    vp.centerSpringX,
    vp.centerSpringY,
    vp.zoomSpring,
    vp.rotationSpring,
    vp.degreesSpring,
  ].filter((s): s is SlowSpring => Boolean(s))
}

// Each viewport spring reads its `animationTime` when a new target is set, so
// lengthening them slows the *next* pan/zoom/rotate. The baseline is cached
// per-spring (not read live) so back-to-back transitions never compound, and
// `restoreSpringAnimationTime` puts manual pan/zoom back to normal speed.
function setSpringAnimationTime(viewer: OpenSeadragon.Viewer, seconds: number): void {
  for (const s of viewportSprings(viewer)) {
    if (s._tourBaseAnimationTime === undefined) s._tourBaseAnimationTime = s.animationTime
    s.animationTime = seconds
  }
}

function restoreSpringAnimationTime(viewer: OpenSeadragon.Viewer): void {
  for (const s of viewportSprings(viewer)) {
    if (s._tourBaseAnimationTime !== undefined) s.animationTime = s._tourBaseAnimationTime
  }
}

// Per-viewer transition bookkeeping.
// - _tourTransitionToken: monotonic, bumped at the start of every transition
//   so a stale chained callback from an interrupted 'smooth_zoom' (e.g. the
//   user clicked Next again mid-glide) becomes a no-op instead of yanking the
//   viewport back to the abandoned target.
// - _tourTransitionDisposers: undo functions for every handler / timer the
//   in-flight transition registered. A superseding transition runs them, as
//   does viewer destruction, so nothing outlives the transition it belongs
//   to. (Otherwise a 'smooth' move to the identical view — where OSD starts
//   no animation and so never raises 'animation-finish' — would leave its
//   finish handler registered; repeated, they would pile up until some later
//   animation finally flushed them.)
// - _tourTransitionDestroyHooked: the one-per-viewer 'destroy' listener that
//   flushes the disposers (it clears pending timers; OSD drops handlers on
//   destroy itself).
type TransitionViewer = OpenSeadragon.Viewer & {
  _tourTransitionToken?: number
  _tourTransitionDisposers?: Array<() => void>
  _tourTransitionDestroyHooked?: boolean
}

function disposeTransition(viewer: OpenSeadragon.Viewer): void {
  const v = viewer as TransitionViewer
  const disposers = v._tourTransitionDisposers
  v._tourTransitionDisposers = []
  for (const dispose of disposers ?? []) {
    try { dispose() } catch { /* viewer already torn down */ }
  }
}

function trackTransitionDisposer(viewer: OpenSeadragon.Viewer, dispose: () => void): void {
  const v = viewer as TransitionViewer
  ;(v._tourTransitionDisposers ??= []).push(dispose)
}

function beginTransition(viewer: OpenSeadragon.Viewer): number {
  const v = viewer as TransitionViewer
  // The previous transition is superseded: drop its pending handlers/timers
  // now rather than leaving token-gated no-ops registered.
  disposeTransition(viewer)
  if (!v._tourTransitionDestroyHooked) {
    v._tourTransitionDestroyHooked = true
    viewer.addOnceHandler('destroy', () => disposeTransition(viewer))
  }
  v._tourTransitionToken = (v._tourTransitionToken ?? 0) + 1
  return v._tourTransitionToken
}

// Run `fn` once the current viewport animation settles, but only if no newer
// transition has superseded `token`. Returns a disposer that unregisters the
// handler (idempotent); it is also run on supersession / viewer destroy, and
// the handler unregisters itself when it fires.
function onceAnimationFinish(viewer: OpenSeadragon.Viewer, token: number, fn: () => void): () => void {
  let active = true
  const dispose = () => {
    if (!active) return
    active = false
    viewer.removeHandler('animation-finish', handler)
  }
  const handler = () => {
    dispose()
    if ((viewer as TransitionViewer)._tourTransitionToken === token) fn()
  }
  viewer.addHandler('animation-finish', handler)
  trackTransitionDisposer(viewer, dispose)
  return dispose
}

// Fallback for the case where a transition is a no-op (target equals the
// current view), so OSD starts no animation and 'animation-finish' never
// fires. Without this the springs would stay stuck at the slow tour speed and
// the next manual pan/zoom would crawl. On expiry it restores the springs and
// runs `disposeFinish` to unregister the finish handler that will now never
// fire. Token-gated so a superseding transition isn't reset early (that
// transition also clears this timer); restoreSpringAnimationTime is
// idempotent, so a redundant restore after a real animation also finishes is
// harmless.
function restoreSpringAnimationTimeAfter(
  viewer: OpenSeadragon.Viewer,
  token: number,
  ms: number,
  disposeFinish: () => void,
): void {
  const timer = setTimeout(() => {
    try {
      if ((viewer as TransitionViewer)._tourTransitionToken !== token) return
      disposeFinish()
      if (viewer.viewport) restoreSpringAnimationTime(viewer)
    } catch { /* viewer destroyed */ }
  }, ms)
  trackTransitionDisposer(viewer, () => clearTimeout(timer))
}

// Axis-aligned union of two viewport rects. Both getBounds() and
// imageToViewportRectangle() return axis-aligned boxes, so a component-wise
// min/max is exact for framing purposes.
function unionRect(a: OpenSeadragon.Rect, b: OpenSeadragon.Rect): OpenSeadragon.Rect {
  const x = Math.min(a.x, b.x)
  const y = Math.min(a.y, b.y)
  const right = Math.max(a.x + a.width, b.x + b.width)
  const bottom = Math.max(a.y + a.height, b.y + b.height)
  return new OpenSeadragon.Rect(x, y, right - x, bottom - y)
}

function padRect(r: OpenSeadragon.Rect, factor: number): OpenSeadragon.Rect {
  const dw = r.width * (factor - 1)
  const dh = r.height * (factor - 1)
  return new OpenSeadragon.Rect(r.x - dw / 2, r.y - dh / 2, r.width + dw, r.height + dh)
}

/**
 * Move the viewport to ``bounds`` (rotated to ``rotationDeg``) for a tour step,
 * animating per ``mode``. Centralising all three modes here guarantees that
 * starting any new transition (even an immediate jump) invalidates an
 * in-flight 'smooth_zoom' chain and restores the springs' normal speed.
 */
export function applyStepTransition(
  viewer: OpenSeadragon.Viewer,
  bounds: OpenSeadragon.Rect,
  rotationDeg: number,
  mode: TransitionMode,
  animationTime: number = TOUR_TRANSITION_ANIMATION_TIME,
): void {
  const token = beginTransition(viewer)

  if (mode === 'none') {
    restoreSpringAnimationTime(viewer)
    viewer.viewport.setRotation(rotationDeg, true)
    viewer.viewport.fitBounds(bounds, true)
    return
  }

  if (mode === 'smooth') {
    setSpringAnimationTime(viewer, animationTime)
    const disposeFinish = onceAnimationFinish(viewer, token, () => restoreSpringAnimationTime(viewer))
    // Safety net for an identically-framed next step (e.g. produced by the
    // editor's Duplicate button): fitBounds/setRotation then animate nothing,
    // so animation-finish never fires and the springs would stay slow — and
    // the finish handler would stay registered, so the timeout drops it too.
    restoreSpringAnimationTimeAfter(viewer, token, animationTime * 1000 + 50, disposeFinish)
    viewer.viewport.setRotation(rotationDeg, false)
    viewer.viewport.fitBounds(bounds, false)
    return
  }

  // smooth_zoom: phase 1 pulls back to an overview framing both the current
  // and target fields (revealing the travel path), then phase 2 pushes into
  // the target. The brief settle at the overview is intentional — it's what
  // lets the viewer read where the tour is heading.
  // Both inputs can carry a rotation (`degrees`), whose x/y is then a rotated
  // corner rather than the top-left, so union their axis-aligned boxes.
  const overview = padRect(
    unionRect(viewer.viewport.getBounds(true).getBoundingBox(), bounds.getBoundingBox()),
    OVERVIEW_PADDING,
  )
  setSpringAnimationTime(viewer, animationTime * PULL_OUT_FRACTION)
  viewer.viewport.setRotation(rotationDeg, false)
  viewer.viewport.fitBounds(overview, false)
  onceAnimationFinish(viewer, token, () => {
    setSpringAnimationTime(viewer, animationTime * (1 - PULL_OUT_FRACTION))
    viewer.viewport.fitBounds(bounds, false)
    onceAnimationFinish(viewer, token, () => restoreSpringAnimationTime(viewer))
  })
}
