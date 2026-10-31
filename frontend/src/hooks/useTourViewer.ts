import { useEffect, useRef, useState } from 'react'
import OpenSeadragon from 'openseadragon'
import { DEFAULT_VIEWER_SETTINGS, bearerHeaders, getToken, getViewerSettings, isAbortError, isStaffFromToken, isSziSlide, type Shape, type ShapeKind, type ShapePoint } from '../lib/api'
import { OSD_PREFIX_URL, osdMaxTilesPerFrame, attachHomeResetsRotation, attachNoFlipKey, attachOpenFailed, attachTwoFingerRotate, attachZoomClamp, loadTileSource } from '../lib/osd'
import { useMouseCoords } from './useMouseCoords'
import { attachScaleBar, slidePixelSize } from '../lib/scalebar'
import {
  applyGraphicCounterRotation,
  applyLabelCounterRotation,
  createPinOverlay,
  normalizePinShape,
  pinPlacement,
} from '../lib/tourPins'
import {
  DEFAULT_FILL_OPACITY,
  applyShapeLabelTransforms,
  createShapeOverlay,
  freehandBounds,
  freehandStroke,
  normalizeShape,
  resolveShapeColor,
  type FreehandStroke,
  type ShapeOverlay,
} from '../lib/tourShapes'
import { makeShapeId, stepPins, type EditState, type Step } from '../lib/tourEditHelpers'
import { orderAnnotations, nextShapeZ } from '../lib/tourLayers'
import { captureStepFrame, fovFromPhysical, stepMpp, stepViewportBounds } from '../lib/tourStepView'

type TourLike = { slide_id: number } | null
type SlideLike = { id?: number; public_id?: string; mpp_x?: number; mpp_y?: number; wsi_filename?: string; tile_version?: number | null } | null

interface Args {
  tour: TourLike
  slide: SlideLike
  debugMode: boolean
  selectedStep: Step | null
  editState: EditState | null
  editingStep: Step | null
  onAddPin: (x: number, y: number) => void
  onAddShape: (shape: Shape) => void
}

// How far apart (in level-0 pixels) two consecutive freehand samples must
// be before both are kept. Filters out pointer jitter so the SVG path stays
// compact without losing visible detail. A very long stroke is thinned
// further, on the fly, so it never exceeds the server's per-shape point cap
// (MAX_FREEHAND_POINTS; see freehandStroke in lib/tourShapes).
const FREEHAND_MIN_SPACING_PX = 1.0

// How far (in screen pixels) the pointer may stray from where it was pressed
// and still place a pin on release. Twice OSD's 5 px clickDistThreshold, so a
// pen or a fingertip that wobbles while pressing still counts as a click,
// while any deliberate pan travels well past it.
const PIN_JITTER_PX = 10

// What the edited-step framing reads off the slide besides the fields: the
// stored-unit factor (stepMpp) and the per-axis pixel size the physical ->
// stored FOV conversion uses (fovFromPhysical). Part of the applied-view memo,
// so a refresh that sets or corrects the slide's MPP re-frames the same
// field strings, which now describe a different field of view.
function viewScaleKey(slide: { mpp_x?: number | null; mpp_y?: number | null }): string {
  const px = slidePixelSize(slide)
  return `${stepMpp(slide)}|${px ? `${px.x}x${px.y}` : ''}`
}

export function useTourViewer({
  tour,
  slide,
  debugMode,
  selectedStep,
  editState,
  editingStep,
  onAddPin,
  onAddShape,
}: Args) {
  const viewerDivRef = useRef<HTMLDivElement | null>(null)
  const scaleBarDivRef = useRef<HTMLDivElement | null>(null)
  const osdRef = useRef<OpenSeadragon.Viewer | null>(null)
  // Viewport captured by the previous OSD instance's cleanup so a same-slide
  // re-init (debug toggle, etc.) restores the user's place. The save MUST
  // happen in cleanup, not at the top of the next effect body: by the time
  // the body runs, React has already invoked the previous cleanup and the
  // viewer has been destroyed, so getBounds() would throw and savedBounds
  // would be lost. Keyed by slideId so a slide-change re-init doesn't bleed
  // the prior slide's viewport onto the new one.
  const savedViewportRef = useRef<{
    slidePublicId: string
    bounds: OpenSeadragon.Rect
    rotation: number
  } | null>(null)
  const dragStartRef = useRef<{ x: number; y: number } | null>(null)
  // Latest pointer position of a rect/circle drag, read by the per-frame
  // draft render.
  const dragCurRef = useRef<{ x: number; y: number } | null>(null)
  const freehandRef = useRef<FreehandStroke | null>(null)
  // Pending requestAnimationFrame for the draft overlay. Pointer events can
  // arrive several times per frame; collecting points stays per event, but
  // the draft (array copy + bounds + React state + SVG rebuild) is rebuilt at
  // most once per frame.
  const draftRafRef = useRef(0)
  const overlayElsRef = useRef<HTMLElement[]>([])
  const labelRotatorsRef = useRef<HTMLElement[]>([])
  const graphicRotatorsRef = useRef<{ el: HTMLElement; baseRot: number }[]>([])
  const shapeOverlaysRef = useRef<ShapeOverlay[]>([])
  // The in-progress drag draft is tracked separately from the committed
  // overlays so a fast freehand drag (one setDraftShape per frame)
  // doesn't tear down and re-add every existing pin/shape on every frame.
  // clearOverlays() leaves draftOverlayElRef alone; the dedicated effect
  // below adds/removes only that single element on draft changes.
  const draftOverlayElRef = useRef<HTMLElement | null>(null)
  const addingPinRef = useRef(false)
  const addingShapeRef = useRef<ShapeKind | null>(null)
  // Defaults for newly-drawn shapes. The user edits color / fill / opacity
  // on the per-shape card after creation rather than configuring presets
  // up front, so these stay constant for the lifetime of the editor.
  const SHAPE_DEFAULTS = {
    stroke_color: 'red',
    fill_enabled: false,
    fill_opacity: DEFAULT_FILL_OPACITY,
    // Freehand strokes smooth by default; the per-shape card can turn it off.
    smoothing: true,
  } as const
  const editStateRef = useRef<EditState | null>(null)
  const lastAppliedViewRef = useRef<{ rotation_deg: string; fov_um_w: string; fov_um_h: string; center_x: string; center_y: string; scale: string } | null>(null)
  const onAddPinRef = useRef(onAddPin)
  const onAddShapeRef = useRef(onAddShape)

  const [addingPin, setAddingPinState] = useState(false)
  const [addingShape, setAddingShapeState] = useState<ShapeKind | null>(null)
  const [pendingPin, setPendingPin] = useState<{ x: number; y: number } | null>(null)
  const [draftShape, setDraftShape] = useState<Shape | null>(null)
  const [osdGen, setOsdGen] = useState(0)
  // Pointer coordinates are shown only in debug / pin / shape modes; tracking
  // them otherwise would re-render the whole editor on every mouse move.
  const mouseCoords = useMouseCoords(osdRef, debugMode || addingPin || !!addingShape, osdGen)
  // Holds an i18n key (the caller translates it), like ViewerPage /
  // TourViewerPage's loadError.
  const [loadError, setLoadError] = useState<string | null>(null)
  // The admin-configured digital-zoom cap. The editor must honour it so its
  // preview matches what the public viewer enforces; without it OSD falls
  // back to its built-in default (1.1), letting the slide zoom ~10% past
  // native even when the admin set the cap to 1.0 (no digital zoom). Default
  // to the same values the public viewers use until the fetch resolves.
  const [maxZoomPixelRatio, setMaxZoomPixelRatio] = useState(DEFAULT_VIEWER_SETTINGS.max_zoom_pixel_ratio)
  // Advanced OpenSeadragon rendering knobs, admin-tunable (see
  // docs/developer-guide.md "Viewer rendering tuning"). Default to OSD's /
  // the app's defaults until the settings fetch resolves so the editor
  // preview matches the public viewer.
  const [immediateRender, setImmediateRender] = useState(DEFAULT_VIEWER_SETTINGS.immediate_render)
  const [maxImageCacheCount, setMaxImageCacheCount] = useState(DEFAULT_VIEWER_SETTINGS.max_image_cache_count)
  // Whether getViewerSettings() has resolved (or failed) at least once. The
  // OSD-init effect below bakes the three settings above into the viewer at
  // construction and lists them in its dependency array, so building with the
  // defaults and then letting the real settings arrive would destroy and
  // rebuild the freshly built viewer (black flash + duplicate tile fetches).
  // Gate the first build on the settings being known so the viewer is built
  // once, with the real values. Never reset to false — settings are global and
  // load once — so a later slide change doesn't re-gate. Mirrors ViewerPage /
  // TourViewerPage.
  const [viewerSettingsLoaded, setViewerSettingsLoaded] = useState(false)
  useEffect(() => {
    const ctrl = new AbortController()
    getViewerSettings({ signal: ctrl.signal })
      .then((s) => {
        if (typeof s.max_zoom_pixel_ratio === 'number') setMaxZoomPixelRatio(s.max_zoom_pixel_ratio)
        if (typeof s.immediate_render === 'boolean') setImmediateRender(s.immediate_render)
        if (typeof s.max_image_cache_count === 'number') setMaxImageCacheCount(s.max_image_cache_count)
        setViewerSettingsLoaded(true)
      })
      .catch((err) => {
        if (isAbortError(err)) return
        // Keep the defaults if settings can't be fetched, but unblock OSD init
        // so a settings outage doesn't leave the editor canvas unbuilt.
        setViewerSettingsLoaded(true)
      })
    return () => ctrl.abort()
  }, [])

  // Pin-placement and shape-drawing are mutually exclusive canvas modes:
  // a single canvas gesture can't mean both "drop a pin here" and "drag a
  // rectangle". Entering either mode therefore cancels the other (and any
  // in-progress shape draft) so the toolbars and the canvas can never be
  // armed for two conflicting actions at once.
  function setAddingPin(v: boolean) {
    addingPinRef.current = v
    setAddingPinState(v)
    if (v) {
      addingShapeRef.current = null
      setAddingShapeState(null)
      resetDrag()
    }
  }
  function setAddingShape(v: ShapeKind | null) {
    addingShapeRef.current = v
    setAddingShapeState(v)
    if (v === null) {
      resetDrag()
    } else {
      addingPinRef.current = false
      setAddingPinState(false)
    }
  }

  // Drop any in-progress drag: its refs, the queued draft frame (which would
  // otherwise re-set the draft after this clears it) and the draft itself.
  function resetDrag() {
    if (draftRafRef.current) {
      cancelAnimationFrame(draftRafRef.current)
      draftRafRef.current = 0
    }
    dragStartRef.current = null
    dragCurRef.current = null
    freehandRef.current = null
    setDraftShape(null)
  }

  // Rebuild the draft from the drag refs on the next animation frame,
  // coalescing however many pointer events arrived in between.
  function scheduleDraftRender() {
    if (draftRafRef.current) return
    draftRafRef.current = requestAnimationFrame(() => {
      draftRafRef.current = 0
      const kind = addingShapeRef.current
      const start = dragStartRef.current
      if (!kind || !start) return
      if (kind === 'freehand') {
        const pts = freehandRef.current?.points() ?? []
        if (pts.length >= 2) setDraftShape(buildDraftFromPoints(pts))
        return
      }
      const cur = dragCurRef.current
      if (!cur) return
      const box = boxFromDrag(kind, start, cur)
      if (box.w <= 0 || box.h <= 0) return
      setDraftShape(buildDraftFromBox(kind, box.x, box.y, box.w, box.h))
    })
  }

  useEffect(() => { addingPinRef.current = addingPin }, [addingPin])
  useEffect(() => { addingShapeRef.current = addingShape }, [addingShape])
  useEffect(() => { editStateRef.current = editState }, [editState])
  useEffect(() => { onAddPinRef.current = onAddPin }, [onAddPin])
  useEffect(() => { onAddShapeRef.current = onAddShape }, [onAddShape])

  function clearOverlays() {
    const viewer = osdRef.current
    if (!viewer) return
    overlayElsRef.current.forEach((el) => {
      try { viewer.removeOverlay(el) } catch {}
    })
    overlayElsRef.current = []
    labelRotatorsRef.current = []
    graphicRotatorsRef.current = []
    shapeOverlaysRef.current = []
  }

  // Counter-rotate every pin label so it stays horizontal regardless of
  // slide rotation, and re-position shape labels/badges against each
  // shape's current rotated AABB. Called both right after rendering and on
  // every viewport update so positions track rotation AND zoom.
  function applyAllLabelCounterRotations() {
    const viewer = osdRef.current
    if (!viewer) return
    const rot = viewer.viewport.getRotation() || 0
    for (const el of labelRotatorsRef.current) {
      applyLabelCounterRotation(el, rot)
    }
    for (const { el, baseRot } of graphicRotatorsRef.current) {
      applyGraphicCounterRotation(el, baseRot, rot)
    }
    for (const overlay of shapeOverlaysRef.current) {
      applyShapeLabelTransforms(overlay, rot)
    }
  }

  function renderStepAnnotations(step: Step | null, localEdit?: EditState | null) {
    const viewer = osdRef.current
    if (!viewer) return
    // Clear first, unconditionally: when the selected step becomes null (e.g.
    // the admin deletes the currently-selected step) the caller re-runs this
    // with step === null, and it must wipe the previous step's pins/shapes
    // rather than leaving them on the canvas as ghosts of a deleted step.
    clearOverlays()
    if (!step) return

    // Show the on-canvas index badge (#1, #2, …) only while editing this
    // step, since that's when the side editor cards exist to cross-reference.
    const showIndex = !!localEdit
    const pins = localEdit ? localEdit.pins : stepPins(step)
    const shapes = localEdit ? localEdit.shapes_l0 : (step.shapes_l0 || []).map(normalizeShape)
    // Add bottom → top in the shared z order. OSD stacks overlays by DOM
    // insertion order (each overlay is wrapped in an absolutely-positioned
    // .openseadragon-overlay-wrapper with no z-index), so adding in z order IS
    // what sets the stacking — pins default above shapes and explicit
    // reordering is honoured. This effect re-runs (clearOverlays + re-add) on
    // any pin/shape/z change, so the insertion order always tracks the current
    // z order. The badge number stays the per-kind array index (matching the
    // editor cards), independent of the paint order.
    orderAnnotations(pins, shapes).forEach((entry) => {
      if (entry.kind === 'pin') {
        const p = entry.item
        const shape = normalizePinShape(p.shape)
        const overlay = createPinOverlay(shape, p.color, p.rotation_deg, p.label, showIndex ? entry.index + 1 : null)
        const pt = viewer.viewport.imageToViewportCoordinates(new OpenSeadragon.Point(p.x, p.y))
        viewer.addOverlay({ element: overlay.root, location: pt, placement: pinPlacement(shape) })
        overlayElsRef.current.push(overlay.root)
        if (overlay.labelRotator) labelRotatorsRef.current.push(overlay.labelRotator)
        if (overlay.graphicRotator) graphicRotatorsRef.current.push({ el: overlay.graphicRotator, baseRot: overlay.graphicBaseRotation })
      } else {
        const sh = entry.item
        const overlay = createShapeOverlay(sh, showIndex ? entry.index + 1 : null)
        const vp = viewer.viewport.imageToViewportRectangle(new OpenSeadragon.Rect(sh.x, sh.y, sh.w, sh.h))
        viewer.addOverlay({ element: overlay.root, location: vp })
        overlayElsRef.current.push(overlay.root)
        if (overlay.labelRotator) shapeOverlaysRef.current.push(overlay)
      }
    })

    applyAllLabelCounterRotations()
  }

  // Turn a press point + current pointer into the shape's bounding box.
  // Rect uses the two points as opposite corners (press = one vertex, release
  // = the diagonal vertex). Circle uses the press point as the CENTRE and the
  // pointer distance as the radius, so the drawn outline follows the cursor —
  // the two-corner box would instead inscribe the circle inside an invisible
  // rectangle whose corners lie outside the circle, making placement hard.
  function boxFromDrag(
    kind: ShapeKind,
    start: { x: number; y: number },
    cur: { x: number; y: number },
  ): { x: number; y: number; w: number; h: number } {
    if (kind === 'circle') {
      const r = Math.hypot(cur.x - start.x, cur.y - start.y)
      return { x: start.x - r, y: start.y - r, w: 2 * r, h: 2 * r }
    }
    return {
      x: Math.min(start.x, cur.x),
      y: Math.min(start.y, cur.y),
      w: Math.abs(cur.x - start.x),
      h: Math.abs(cur.y - start.y),
    }
  }

  function buildDraftFromBox(kind: ShapeKind, x: number, y: number, w: number, h: number): Shape {
    return {
      id: 'draft',
      kind,
      x, y, w, h,
      stroke_color: SHAPE_DEFAULTS.stroke_color,
      fill_enabled: SHAPE_DEFAULTS.fill_enabled,
      fill_opacity: SHAPE_DEFAULTS.fill_opacity,
      points: [],
      smoothing: SHAPE_DEFAULTS.smoothing,
      label: '',
      label_position: 'above',
      z: 0,
    }
  }

  // `points` must be a fresh array (FreehandStroke.points() returns one): it
  // becomes the draft state as-is.
  function buildDraftFromPoints(points: ShapePoint[]): Shape {
    const bb = freehandBounds(points)
    return {
      id: 'draft',
      kind: 'freehand',
      x: bb.x, y: bb.y, w: bb.w, h: bb.h,
      stroke_color: SHAPE_DEFAULTS.stroke_color,
      fill_enabled: SHAPE_DEFAULTS.fill_enabled,
      fill_opacity: SHAPE_DEFAULTS.fill_opacity,
      points,
      smoothing: SHAPE_DEFAULTS.smoothing,
      label: '',
      label_position: 'above',
      z: 0,
    }
  }

  // OSD lifecycle: (re)create viewer when tour, debugMode, or the slide's
  // SZI status changes. The slide format must be known before constructing
  // the viewer because SZI requires an async tile-source factory call.
  const isSzi = isSziSlide(slide as any)
  useEffect(() => {
    if (!tour || !viewerDivRef.current) return
    if (!slide) return
    // Wait for the real viewer settings before the first build so the viewer
    // isn't constructed with the default max_zoom_pixel_ratio / immediate_render
    // / max_image_cache_count and then torn down + rebuilt when they resolve.
    if (!viewerSettingsLoaded) return
    // Tiles are addressed by the slide's public_id token (never the numeric id).
    const slidePublicId = slide.public_id ?? ''
    let cancelled = false

    // The tour editor is admin-only and previews drafts; pass the admin
    // bearer token so the /dzi/ publish gate accepts the tile fetches.
    const adminToken = isStaffFromToken() ? getToken() : null
    const ajaxHeaders = adminToken ? bearerHeaders(adminToken) : undefined
    // The WSI generation versions every tile URL (see lib/tileUrl); it is in
    // the deps below so a replaced slide re-opens on its new URLs.
    const tileVersion = slide.tile_version
    let cleanup: (() => void) | null = null
    ;(async () => {
      const result = await loadTileSource(slidePublicId, isSzi, ajaxHeaders, tileVersion)
      if (!result.ok) {
        // Surface the failure so the editor shows a banner instead of a
        // silent black canvas. Mirrors ViewerPage / TourViewerPage handling.
        if (!cancelled) {
          setLoadError(
            result.reason === 'szi-plugin-missing'
              ? 'viewer.sziPluginMissing'
              : 'viewer.sziOpenFailed'
          )
        }
        return
      }
      if (cancelled || !viewerDivRef.current) return
      setLoadError(null)
      const v = OpenSeadragon({
        element: viewerDivRef.current,
        prefixUrl: OSD_PREFIX_URL,
        tileSources: result.tileSource,
        showRotationControl: true,
        showNavigator: true,
        // Honour the admin's configured digital-zoom cap so the editor
        // preview matches the public viewer. Omitting this lets OSD use its
        // 1.1 default and zoom past native even when the cap is 1.0.
        maxZoomPixelRatio,
        // Fetch display-resolution tiles first, skipping in-between levels
        // (see ViewerPage for the trade-off). Admin-tunable via Advanced
        // rendering settings (default true).
        immediateRender,
        maxImageCacheCount,
        maxTilesPerFrame: osdMaxTilesPerFrame(),
        gestureSettingsMouse: { clickToZoom: false },
        // Rotation is handled by attachTwoFingerRotate, not OSD's
        // distance-gated pinchRotate (see the helper for why).
        ...(ajaxHeaders ? { loadTilesWithAjax: true, ajaxHeaders } : {}),
        debugMode,
      })
      osdRef.current = v
      // A DZI descriptor that fails to load gets the editor's translated
      // banner instead of OSD's raw English message (see attachOpenFailed).
      const detachOpenFailed = attachOpenFailed(v, () => setLoadError('viewer.slideLoadFailed'))
      v.addOnceHandler('open', () => {
        // Restore viewport from the previous OSD instance for THIS slide.
        // The slideId tag guards against bleeding slide A's viewport onto
        // slide B (different image coordinate spaces).
        const saved = savedViewportRef.current
        if (saved && saved.slidePublicId === slidePublicId) {
          try {
            v.viewport.setRotation(saved.rotation)
            v.viewport.fitBounds(saved.bounds, true)
          } catch {}
        }
        savedViewportRef.current = null
        setOsdGen((g) => g + 1)
      })

      // Pin placement tells a click from a gesture by what happened since the
      // press. OSD raises canvas-click on EVERY release over the canvas,
      // including the end of a drag-pan and the last finger lifting after a
      // pinch, so placing on any click would drop the pin wherever a pan ended.
      // OSD's own `quick` flag is no substitute: it also needs the release
      // within 300 ms and 5 px of the press, which rejects a deliberate slow
      // click, a short touch-hold, or a pen that wobbles a few pixels — with
      // no feedback — while a quick two-finger tap can still pass. Instead:
      // a press starts a candidate click; a drag past PIN_JITTER_PX from the
      // press, a pinch, or a second pointer landing cancels it. A cancelled
      // release leaves pin mode armed, so the click that follows places it.
      let pressPos: { x: number; y: number } | null = null
      let gestureMoved = false
      v.addHandler('canvas-press', (ev: any) => {
        pressPos = { x: ev.position.x, y: ev.position.y }
        gestureMoved = false
      })
      v.addHandler('canvas-drag', (ev: any) => {
        if (gestureMoved) return
        // canvas-drag fires on every move while pressed, even 1 px.
        if (!pressPos || Math.hypot(ev.position.x - pressPos.x, ev.position.y - pressPos.y) > PIN_JITTER_PX) {
          gestureMoved = true
        }
      })
      v.addHandler('canvas-pinch', () => { gestureMoved = true })
      // canvas-press only fires for the first contact and canvas-pinch only
      // once the fingers move, so a two-finger tap is caught at the second
      // pointer's own pointerdown (capture phase on the container, so it is
      // seen before OSD's canvas tracker handles it).
      const onPointerDown = (e: Event) => {
        if ((e as PointerEvent).isPrimary === false) gestureMoved = true
      }
      v.element.addEventListener('pointerdown', onPointerDown, true)
      const detachPinGesture = () => v.element.removeEventListener('pointerdown', onPointerDown, true)

      v.addHandler('canvas-click', (ev: any) => {
        const isClick = pressPos !== null && !gestureMoved
        pressPos = null
        if (!addingPinRef.current || !isClick) return
        const webPoint = ev.position
        const viewportPoint = v.viewport.pointFromPixel(webPoint)
        const imgPoint = v.viewport.viewportToImageCoordinates(viewportPoint)
        onAddPinRef.current(imgPoint.x, imgPoint.y)
        setPendingPin({ x: imgPoint.x, y: imgPoint.y })
        setAddingPin(false)
        ev.preventDefaultAction = true
      })

      v.addHandler('canvas-press', (ev: any) => {
        const kind = addingShapeRef.current
        if (!kind) return
        const viewportPoint = v.viewport.pointFromPixel(ev.position)
        const imgPoint = v.viewport.viewportToImageCoordinates(viewportPoint)
        dragStartRef.current = { x: imgPoint.x, y: imgPoint.y }
        dragCurRef.current = null
        freehandRef.current = kind === 'freehand'
          ? freehandStroke({ x: imgPoint.x, y: imgPoint.y }, FREEHAND_MIN_SPACING_PX)
          : null
        ev.preventDefaultAction = true
      })

      v.addHandler('canvas-drag', (ev: any) => {
        const kind = addingShapeRef.current
        if (!kind || !dragStartRef.current) return
        const viewportPoint = v.viewport.pointFromPixel(ev.position)
        const imgPoint = v.viewport.viewportToImageCoordinates(viewportPoint)
        if (kind === 'freehand') {
          // Sample on every event (so no pointer movement is lost), render per
          // frame. The stroke object enforces the point cap without cutting
          // the curve short, so the draft is exactly what drag-end commits.
          if (freehandRef.current?.add({ x: imgPoint.x, y: imgPoint.y })) scheduleDraftRender()
        } else {
          dragCurRef.current = { x: imgPoint.x, y: imgPoint.y }
          scheduleDraftRender()
        }
        ev.preventDefaultAction = true
      })

      // A second finger turns the gesture into a pinch-zoom, which OSD still
      // applies in shape mode. canvas-press only fires for the first finger,
      // which has usually moved a little before the second one lands, so a
      // drag is already under way and the lift at the end raises
      // canvas-drag-end: without this, a pinch would commit a stray shape from
      // the press point to wherever the last finger left the (now re-zoomed)
      // canvas. Drop the drag instead; shape mode stays armed for the drag
      // the user then makes, and the fingers still down just pan.
      v.addHandler('canvas-pinch', () => {
        if (addingShapeRef.current && dragStartRef.current) resetDrag()
      })

      v.addHandler('canvas-drag-end', (ev: any) => {
        const kind = addingShapeRef.current
        if (!kind || !dragStartRef.current || !editStateRef.current) return
        // Capture all in-flight state BEFORE exiting shape mode, since
        // setAddingShape(null) clears the freehand stroke ref and the drag
        // start ref via its centralised cleanup branch. points() is already
        // capped at MAX_FREEHAND_POINTS, so the server will accept it.
        const start = dragStartRef.current
        const points = kind === 'freehand' ? (freehandRef.current?.points() ?? []) : []
        const baseStyle = {
          stroke_color: SHAPE_DEFAULTS.stroke_color,
          fill_enabled: SHAPE_DEFAULTS.fill_enabled,
          fill_opacity: SHAPE_DEFAULTS.fill_opacity,
          // Freehand-only in effect; rect/circle carry it harmlessly.
          smoothing: SHAPE_DEFAULTS.smoothing,
        }

        // Always exit shape mode on drag-end — a tap or too-small drag is a
        // cancel, and the user can click the kind button again to retry.
        setAddingShape(null)

        // Newly-drawn shapes stack above existing shapes but below pins, so the
        // "pins above shapes" default holds even when a shape is drawn last.
        const shapeZ = nextShapeZ(editStateRef.current.pins, editStateRef.current.shapes_l0)

        if (kind === 'freehand') {
          if (points.length < 2) return
          const bb = freehandBounds(points)
          onAddShapeRef.current({
            id: makeShapeId(),
            kind: 'freehand',
            x: bb.x, y: bb.y, w: bb.w, h: bb.h,
            ...baseStyle,
            points,
            label: '',
            label_position: 'above',
            z: shapeZ,
          })
        } else {
          const viewportPoint = v.viewport.pointFromPixel(ev.position)
          const imgPoint = v.viewport.viewportToImageCoordinates(viewportPoint)
          const box = boxFromDrag(kind, start, imgPoint)
          if (box.w <= 1 || box.h <= 1) return
          onAddShapeRef.current({
            id: makeShapeId(),
            kind,
            x: box.x, y: box.y, w: box.w, h: box.h,
            ...baseStyle,
            points: [],
            label: '',
            label_position: 'above',
            z: shapeZ,
          })
        }
        ev.preventDefaultAction = true
      })

      // Make the Go Home button a full reset (pan + zoom + rotation).
      const detachHomeReset = attachHomeResetsRotation(v)
      // Hold the digital-zoom cap on every zoom event (pinch/wheel can
      // overshoot maxZoomPixelRatio), same as the public viewers.
      const detachZoomClamp = attachZoomClamp(v)
      const detachRotate = attachTwoFingerRotate(v)
      // No mirror-image slide from a stray ``f`` key.
      const detachNoFlip = attachNoFlipKey(v)

      // Track viewport updates (rotate + zoom + pan) so pin labels stay
      // horizontal AND shape labels/badges keep tracking their shapes'
      // current rotated AABB in screen pixels (which scales with zoom).
      // rAF-coalesce: update-viewport fires every animation frame; batching
      // into a single rAF prevents redundant DOM read/write cycles per frame.
      let rafId = 0
      const onViewportUpdate = () => {
        if (rafId) return
        rafId = requestAnimationFrame(() => {
          rafId = 0
          applyAllLabelCounterRotations()
        })
      }
      v.addHandler('update-viewport', onViewportUpdate)

      cleanup = () => {
        // Capture the viewport BEFORE destroying so the next effect run
        // (debug toggle, etc.) can restore it. Tagged with the slide token so
        // a slide-change cleanup can't bleed into the new slide's viewport.
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
        detachPinGesture()
        detachHomeReset()
        detachZoomClamp()
        detachRotate()
        detachNoFlip()
        v.removeHandler('update-viewport', onViewportUpdate)
        if (rafId) { cancelAnimationFrame(rafId); rafId = 0 }
        // A queued draft frame would add its overlay to the destroyed viewer.
        if (draftRafRef.current) { cancelAnimationFrame(draftRafRef.current); draftRafRef.current = 0 }
        v.destroy()
        // Null the ref so the next effect body and any concurrent
        // step/overlay effects don't operate on a destroyed viewer.
        if (osdRef.current === v) osdRef.current = null
      }
    })()

    return () => {
      cancelled = true
      if (cleanup) cleanup()
    }
    // Track only the slide identity, not the `tour` object reference: the
    // editor's refresh() replaces `tour` with a fresh object after every save,
    // and depending on that ref would tear down OSD and re-fetch tiles on
    // every save — visible as a black flash in the editor. (tile_version is a
    // scalar that moves only on a WSI replace, so it re-opens just then.)
  }, [tour?.slide_id, debugMode, isSzi, slide?.id, slide?.tile_version, viewerSettingsLoaded, maxZoomPixelRatio, immediateRender, maxImageCacheCount])

  // Scale bar attaches once OSD's "open" event fires (osdGen bump).
  useEffect(() => {
    if (!slide || !osdRef.current || !scaleBarDivRef.current) return
    // Per-axis MPP: correct on a rotated anisotropic slide (see lib/scalebar).
    const mpp = slidePixelSize(slide)
    if (!mpp) return
    const detach = attachScaleBar({ viewer: osdRef.current, mpp, container: scaleBarDivRef.current as HTMLDivElement })
    return () => { try { detach() } catch {} }
  }, [slide?.mpp_x, slide?.mpp_y, tour?.slide_id, osdGen])

  // Re-render committed annotations whenever the selected step / edit
  // changes, or when OSD finishes loading (osdGen bump) so a race between
  // step selection and tile-source open doesn't leave the canvas
  // annotation-free. The in-progress draft is handled by the next effect
  // so a freehand drag doesn't churn through every committed overlay.
  useEffect(() => {
    // Only feed the draft editState when the step on screen IS the step being
    // edited. Peeking at another step (selectedStep != editingStep) must draw
    // that step's committed pins/shapes, not the edited step's uncommitted draft.
    renderStepAnnotations(selectedStep, selectedStep?.id === editingStep?.id ? editState : null)
  }, [selectedStep, editState, editingStep, osdGen])

  // Manage the single draft overlay independently of the committed render
  // path. Runs on every draftShape mutation (at most once per animation
  // frame during a drag; see scheduleDraftRender) but only touches OSD's
  // overlay list for the draft itself.
  useEffect(() => {
    const viewer = osdRef.current
    if (!viewer) return
    if (draftOverlayElRef.current) {
      try { viewer.removeOverlay(draftOverlayElRef.current) } catch {}
      draftOverlayElRef.current = null
    }
    if (!draftShape) return
    const draftOverlay = createShapeOverlay(draftShape)
    // Tint outline so the in-progress draft is visually distinct from
    // committed shapes (which only carry the standard border).
    draftOverlay.root.style.outline = `1px solid ${resolveShapeColor(draftShape.stroke_color)}`
    // The draft always paints above committed overlays: this effect runs after
    // the committed-render effect, so its overlay is appended last, and OSD
    // stacks overlays by insertion order.
    const vp = viewer.viewport.imageToViewportRectangle(
      new OpenSeadragon.Rect(draftShape.x, draftShape.y, draftShape.w, draftShape.h),
    )
    viewer.addOverlay({ element: draftOverlay.root, location: vp })
    draftOverlayElRef.current = draftOverlay.root
  }, [draftShape, osdGen])

  // While editing a step, sync the canvas viewport to the edited center/FOV.
  // The editor's FOV fields hold physical µm; convert them to the stored
  // units stepViewportBounds frames with (identity for square pixels; see
  // lib/tourStepView).
  //
  // The effect also re-runs whenever `editingStep` or `slide` is merely a new
  // object with the same values ("Save as preset", any refresh(), ↑/↓ or
  // delete renumbering the steps), so what it frames is remembered in
  // lastAppliedViewRef (the five field strings plus the slide's scale, see
  // viewScaleKey) and only a CHANGED view is framed again. Without that,
  // each such re-render would snap a viewport the user had panned away back onto
  // the edited step. openEditor clears the memo, so opening a step always
  // frames it; "apply current view" records what it captured, which is
  // already on screen.
  useEffect(() => {
    if (!editingStep || !editState || !slide || !osdRef.current) return
    const rotStr = editState.rotation_deg.trim()
    const wStr = editState.fov_um_w.trim()
    const hStr = editState.fov_um_h.trim()
    const cxStr = editState.center_x.trim()
    const cyStr = editState.center_y.trim()
    const rot = Number(rotStr)
    const fovUmW = Number(wStr)
    const fovUmH = Number(hStr)
    const cx = Number(cxStr)
    const cy = Number(cyStr)
    if (
      !rotStr || !wStr || !hStr || !cxStr || !cyStr
      || !Number.isFinite(rot) || !Number.isFinite(fovUmW) || !Number.isFinite(fovUmH)
      || !Number.isFinite(cx) || !Number.isFinite(cy)
      || fovUmW <= 0 || fovUmH <= 0
    ) {
      // Nothing to frame while a field is blank or invalid — and forget what
      // was framed, so retyping the very value that was there (clear, then
      // type it back) frames it again instead of matching the stale memo.
      lastAppliedViewRef.current = null
      return
    }
    const scale = viewScaleKey(slide)
    const last = lastAppliedViewRef.current
    if (
      last && last.rotation_deg === rotStr && last.fov_um_w === wStr && last.fov_um_h === hStr
      && last.center_x === cxStr && last.center_y === cyStr && last.scale === scale
    ) {
      return
    }
    const viewer = osdRef.current
    const fov = fovFromPhysical(slide, rot, { w: fovUmW, h: fovUmH })
    const vpRect = stepViewportBounds(viewer.viewport, {
      center_x: cx, center_y: cy, fov_um_w: fov.w, fov_um_h: fov.h, rotation_deg: rot,
    }, stepMpp(slide))
    viewer.viewport.setRotation(rot)
    viewer.viewport.fitBounds(vpRect, true)
    lastAppliedViewRef.current = { rotation_deg: rotStr, fov_um_w: wStr, fov_um_h: hStr, center_x: cxStr, center_y: cyStr, scale }
  }, [editingStep, editState?.rotation_deg, editState?.fov_um_w, editState?.fov_um_h, editState?.center_x, editState?.center_y, slide])

  function jumpToStep(s: Step) {
    if (!slide || !osdRef.current) return
    const viewer = osdRef.current
    const vpRect = stepViewportBounds(viewer.viewport, s, stepMpp(slide))
    viewer.viewport.setRotation(s.rotation_deg || 0)
    viewer.viewport.fitBounds(vpRect, true)
  }

  function captureCurrentView() {
    if (!osdRef.current || !slide) return null
    // Exact inverse of stepViewportBounds (see lib/tourStepView), so applying
    // the captured values reproduces this framing at any rotation, and slides
    // without an MPP record the real field instead of a fixed placeholder.
    // The FOV is in stored units: what a new step persists as-is, and what
    // the editor converts with fovToPhysical before showing it.
    return captureStepFrame(osdRef.current.viewport, stepMpp(slide))
  }

  function resetLastAppliedView() {
    lastAppliedViewRef.current = null
  }

  function rememberAppliedView(view: { rotation_deg: string; fov_um_w: string; fov_um_h: string; center_x: string; center_y: string }) {
    lastAppliedViewRef.current = { ...view, scale: slide ? viewScaleKey(slide) : '' }
  }

  function cancelDraft() {
    resetDrag()
  }

  return {
    viewerDivRef,
    scaleBarDivRef,
    addingPin,
    setAddingPin,
    addingShape,
    setAddingShape,
    pendingPin,
    setPendingPin,
    draftShape,
    cancelDraft,
    mouseCoords,
    loadError,
    jumpToStep,
    captureCurrentView,
    resetLastAppliedView,
    rememberAppliedView,
  }
}
