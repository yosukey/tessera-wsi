import type { Shape, ShapeLabelPosition, ShapeKind, ShapePoint } from './api'
import {
  normalizeRotationDeg,
  buildIndexBadge,
  buildLabelElement,
  PIN_COLORS,
  PIN_COLOR_ORDER,
} from './tourPins'
import type { PinColorName } from './tourPins'

export const SHAPE_LABEL_POSITIONS: ShapeLabelPosition[] = ['above', 'below', 'center']
export const SHAPE_KINDS: ShapeKind[] = ['rect', 'circle', 'freehand']

export type ShapeColorName = PinColorName
export const SHAPE_COLORS: Record<ShapeColorName, string> = PIN_COLORS
export const SHAPE_COLOR_ORDER: ShapeColorName[] = PIN_COLOR_ORDER

export const DEFAULT_FILL_OPACITY = 0.5

export function normalizeShapeLabelPosition(raw: string | undefined | null): ShapeLabelPosition {
  return raw === 'below' || raw === 'center' ? raw : 'above'
}

export function normalizeShapeKind(raw: string | undefined | null): ShapeKind {
  return raw === 'circle' || raw === 'freehand' ? raw : 'rect'
}

// CSS hex color literal (#RGB / #RGBA / #RRGGBB / #RRGGBBAA). Stroke color
// is interpolated into SVG attributes (path stroke=...) and CSS strings
// (border, background) downstream, so anything outside this whitelist is
// rejected to prevent attribute / markup injection from a malicious stored
// value. The named-palette branch returns a constant and is safe.
const SAFE_HEX_COLOR = /^#[0-9a-fA-F]{3,8}$/

// Resolve a stored stroke_color (preset name OR hex string) into a CSS color.
// Unknown / non-hex values fall back to the default palette color rather
// than being passed through verbatim.
export function resolveShapeColor(name: string | undefined | null): string {
  if (!name) return PIN_COLORS.red
  // hasOwnProperty (not `in`): the latter also matches Object.prototype keys
  // like "constructor"/"toString", so a stored color of "constructor" would
  // resolve to a function and produce a garbage annotation.
  if (Object.prototype.hasOwnProperty.call(PIN_COLORS, name)) return PIN_COLORS[name as ShapeColorName]
  if (SAFE_HEX_COLOR.test(name)) return name
  return PIN_COLORS.red
}

// Most vertices one freehand stroke may carry. Mirrors
// services/app/wsi_app/schemas.py:_MAX_FREEHAND_POINTS — the server rejects
// (422) a shape with more, and since a step save sends the whole shapes_l0
// list, one over-long stroke fails the entire save. Keep the two in sync.
export const MAX_FREEHAND_POINTS = 4096

// Arc-length spacing that leaves at most `target` vertices of a stroke of
// length `len`: decimateByArc keeps the first vertex plus at most one per
// whole multiple of the spacing, i.e. at most len / spacing + 1 = target.
// (The 1e-9 absorbs float rounding so the bound holds exactly.)
function spacingFor(len: number, target: number): number {
  return target > 1 ? (len / (target - 1)) * (1 + 1e-9) : Infinity
}

// Path left over past the last threshold once a vertex is kept. Thresholds
// sit at whole multiples of `spacing` along the path, so the remainder carries
// over (resetting it to 0 instead would waste budget whenever the sample gap
// is just under the spacing — every other sample would go).
function arcRemainder(acc: number, spacing: number): number {
  return spacing > 0 ? acc % spacing : 0
}

// Uniform arc-length decimation: keep the first sample, then the first sample
// past each whole multiple of `spacing` of path. The vertices stay real
// pointer samples, evenly spread along the curve, so
//  - a dropped sample lies within one spacing of path, hence of distance, of
//    the kept vertex before it (bounded error),
//  - the vertex count is predictable — at most len / spacing + 1, and close to
//    it — unlike a chord-distance filter, whose count jumps unpredictably on
//    wiggly strokes and can thin a stroke far below the budget, and
//  - the smoothing renderer, which resamples at the median vertex gap capped
//    to MAX_SMOOTH_POINTS, sees essentially the same path and still hits its
//    cap, so a smoothed stroke renders as it would undecimated.
// The last sample is NOT forced in (callers append it). `carry` returns the
// path accumulated past the last threshold, for incremental continuation.
function decimateByArc(points: ShapePoint[], spacing: number): { kept: ShapePoint[]; carry: number } {
  if (!points.length) return { kept: [], carry: 0 }
  const kept = [points[0]]
  let acc = 0
  for (let i = 1; i < points.length; i++) {
    acc += Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y)
    if (acc >= spacing && acc > 0) {
      kept.push(points[i])
      acc = arcRemainder(acc, spacing)
    }
  }
  return { kept, carry: acc }
}

function pathLength(points: ShapePoint[]): number {
  let len = 0
  for (let i = 1; i < points.length; i++) {
    len += Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y)
  }
  return len
}

// When a stroke outgrows its budget it is re-decimated down to this share of
// it, so a long stroke re-decimates only after ~1/3 more path (O(log n)
// passes in total) instead of on every new vertex once it sits at the limit.
const COARSEN_TARGET = 0.75

// Incremental freehand sampler that never yields more than `max` vertices
// and never cuts the stroke short. Samples closer than `minSpacing` to the
// previous one are dropped as pointer jitter; below the budget every remaining
// sample is kept. Once the budget would be
// exceeded, the stroke is re-decimated (decimateByArc) from the
// full-resolution samples — not from already-thinned vertices, so the error
// stays bounded by the current spacing instead of accumulating — and later
// samples continue at that spacing. The latest sample is always the last
// vertex, so the committed curve ends exactly where the pointer was released:
// dropping the tail instead would visibly cut the curve short.
export type FreehandStroke = {
  // Returns true when the stroke changed (the sample was not jitter).
  add(p: ShapePoint): boolean
  // The stroke to draw / commit: at most `max` vertices, ending at the
  // latest sample. A fresh array each call.
  points(): ShapePoint[]
}

export function freehandStroke(
  start: ShapePoint,
  minSpacing: number,
  max: number = MAX_FREEHAND_POINTS,
): FreehandStroke {
  const raw: ShapePoint[] = [start]
  let kept: ShapePoint[] = [start]
  let len = 0
  let spacing = 0 // path between kept-vertex thresholds; 0 = keep every sample
  let carry = 0 // path accumulated past the last threshold
  // One slot is reserved for the live end point that points() appends.
  const budget = Math.max(1, max - 1)
  return {
    add(p) {
      const last = raw[raw.length - 1]
      const d = Math.hypot(p.x - last.x, p.y - last.y)
      if (d < minSpacing || d === 0) return false
      raw.push(p)
      len += d
      carry += d
      if (carry >= spacing) {
        kept.push(p)
        carry = arcRemainder(carry, spacing)
        if (kept.length > budget) {
          spacing = spacingFor(len, Math.floor(budget * COARSEN_TARGET))
          ;({ kept, carry } = decimateByArc(raw, spacing))
        }
      }
      return true
    },
    points() {
      const end = raw[raw.length - 1]
      return kept[kept.length - 1] === end ? kept.slice() : [...kept, end]
    },
  }
}

// Reduce an arbitrary stroke to at most `max` vertices with the same
// decimation as the drawing tool, keeping its first and last vertex. Returns
// the input array untouched when it already fits, so the common path costs a
// length check.
export function capFreehandPoints(points: ShapePoint[], max: number = MAX_FREEHAND_POINTS): ShapePoint[] {
  if (points.length <= max) return points
  // max - 1 decimated vertices + the forced last one.
  const { kept } = decimateByArc(points, spacingFor(pathLength(points), Math.max(1, max - 1)))
  const end = points[points.length - 1]
  return kept[kept.length - 1] === end ? kept : [...kept, end]
}

// Index of the first freehand shape whose point list exceeds the server cap,
// or -1. The editor checks this before saving so an offending shape is
// named (by its card number) instead of the whole save failing with a 422.
export function findOverCapShape(shapes: Shape[], max: number = MAX_FREEHAND_POINTS): number {
  return shapes.findIndex((s) => Array.isArray(s.points) && s.points.length > max)
}

// Fill in defaults for partial shape records (e.g. just-drawn drafts that
// haven't been through the editor yet) so the renderer always sees a
// canonical Shape. Freehand points are capped at MAX_FREEHAND_POINTS (an
// error-bounded decimation, not a truncation) so no shape that enters the
// editor's state can be one the server will refuse to save.
export function normalizeShape(raw: Partial<Shape> & {
  id: string
  x: number; y: number; w: number; h: number
}): Shape {
  const kind = normalizeShapeKind(raw.kind)
  const points = Array.isArray(raw.points) ? capFreehandPoints(raw.points) : []
  return {
    id: raw.id,
    kind,
    x: raw.x,
    y: raw.y,
    w: raw.w,
    h: raw.h,
    stroke_color: raw.stroke_color || 'red',
    fill_enabled: typeof raw.fill_enabled === 'boolean' ? raw.fill_enabled : false,
    fill_opacity: typeof raw.fill_opacity === 'number' ? raw.fill_opacity : DEFAULT_FILL_OPACITY,
    points: kind === 'freehand' ? points : [],
    // Freehand smoothing defaults on: a draft without the field is treated as
    // smoothed, matching the DB column default.
    smoothing: typeof raw.smoothing === 'boolean' ? raw.smoothing : true,
    label: raw.label || '',
    label_position: normalizeShapeLabelPosition(raw.label_position),
    // Shapes default to the bottom of the z scale so pins (LAYER_PIN_BASE)
    // paint above them unless explicitly reordered. See lib/tourLayers.
    z: typeof raw.z === 'number' ? raw.z : 0,
  }
}

// Minimum bounding box for a freehand stroke. Pads degenerate strokes (a
// single point or a perfectly axis-aligned line) so the resulting overlay
// has non-zero size and OSD can render it.
export function freehandBounds(points: ShapePoint[]): { x: number; y: number; w: number; h: number } {
  if (!points.length) return { x: 0, y: 0, w: 1, h: 1 }
  let minX = points[0].x, minY = points[0].y, maxX = points[0].x, maxY = points[0].y
  for (let i = 1; i < points.length; i++) {
    const p = points[i]
    if (p.x < minX) minX = p.x
    if (p.x > maxX) maxX = p.x
    if (p.y < minY) minY = p.y
    if (p.y > maxY) maxY = p.y
  }
  const w = Math.max(maxX - minX, 1)
  const h = Math.max(maxY - minY, 1)
  return { x: minX, y: minY, w, h }
}


// Compose stroke / fill CSS for a shape. Returns the resolved stroke color
// (so SVG renderers can apply it directly) plus a fill string suitable for
// `background:` (rect/circle) or SVG `fill=`.
function shapeStyle(shape: Shape): { stroke: string; fill: string } {
  const stroke = resolveShapeColor(shape.stroke_color)
  if (!shape.fill_enabled) {
    return { stroke, fill: 'transparent' }
  }
  const opacity = Math.max(0, Math.min(1, shape.fill_opacity))
  return { stroke, fill: hexToRgba(stroke, opacity) }
}

// The fill for a resolved stroke color at the given opacity. The input is
// always a hex literal (resolveShapeColor maps a preset name to its hex and
// lets only #RGB / #RGBA / #RRGGBB / #RRGGBBAA through), so #RGB / #RRGGBB
// become a plain rgba(); a hex that carries its own alpha channel goes through
// CSS color-mix instead, which scales that alpha by the fill opacity.
function hexToRgba(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color.trim())
  if (m) {
    let hex = m[1]
    if (hex.length === 3) hex = hex.split('').map((c) => c + c).join('')
    const r = parseInt(hex.slice(0, 2), 16)
    const g = parseInt(hex.slice(2, 4), 16)
    const b = parseInt(hex.slice(4, 6), 16)
    return `rgba(${r}, ${g}, ${b}, ${alpha})`
  }
  // #RGBA / #RRGGBBAA. CSS color-mix (supported by every major browser since
  // 2023) keeps the color's own alpha and scales it by the fill opacity.
  return `color-mix(in srgb, ${color} ${Math.round(alpha * 100)}%, transparent)`
}

// A shape overlay returned by createShapeOverlay.
//   - root: the element OSD positions and sizes; it rotates with the slide.
//   - labelRotator: a 0×0 wrapper at the root's center that holds the label
//     and badge. Counter-rotated by the viewport rotation so its children
//     stay screen-horizontal. null when there is nothing to rotate.
//   - labelEl / badgeEl: held so applyShapeLabelTransforms can update their
//     pixel offsets as the rotated AABB changes (rotation OR zoom).
export type ShapeOverlay = {
  root: HTMLElement
  labelRotator: HTMLElement | null
  labelEl: HTMLElement | null
  labelPosition: ShapeLabelPosition
  badgeEl: HTMLElement | null
}

// 0×0 anchor at the shape's center. Rotating it by -viewportRotationDeg
// pivots its children around the shape center, keeping text horizontal in
// screen space while preserving each child's screen-pixel offset (set by
// applyShapeLabelTransforms).
function buildShapeLabelRotator(): HTMLElement {
  const wrap = document.createElement('div')
  wrap.style.position = 'absolute'
  wrap.style.left = '50%'
  wrap.style.top = '50%'
  wrap.style.width = '0'
  wrap.style.height = '0'
  wrap.style.pointerEvents = 'none'
  return wrap
}

// Build the OSD overlay for a shape. `root` is sized by OSD to fit the
// level-0 bounding box; the optional rotator wraps label/badge so they stay
// readable and correctly placed against the rotated AABB.
export function createShapeOverlay(shape: Shape, index: number | null = null): ShapeOverlay {
  const kind = normalizeShapeKind(shape.kind)
  const { stroke, fill } = shapeStyle(shape)
  const labelPosition = normalizeShapeLabelPosition(shape.label_position)
  const labelEl = buildLabelElement(shape.label)
  const badgeEl = index !== null ? buildIndexBadge(index, stroke) : null

  let root: HTMLElement
  if (kind === 'circle') {
    root = document.createElement('div')
    root.style.border = `2px dashed ${stroke}`
    root.style.borderRadius = '50%'
    root.style.background = fill
    root.style.pointerEvents = 'none'
    root.style.boxSizing = 'border-box'
  } else if (kind === 'freehand') {
    // The outer element occupies the freehand bounding box; the SVG inside
    // uses the same bbox as its viewBox so points draw at their level-0
    // positions and scale together with OSD's overlay sizing.
    root = document.createElement('div')
    root.style.position = 'relative'
    root.style.pointerEvents = 'none'
    const points = Array.isArray(shape.points) ? shape.points : []
    if (points.length >= 2) {
      const ns = 'http://www.w3.org/2000/svg'
      const svg = document.createElementNS(ns, 'svg')
      svg.setAttribute('width', '100%')
      svg.setAttribute('height', '100%')
      svg.setAttribute('viewBox', `${shape.x} ${shape.y} ${shape.w} ${shape.h}`)
      svg.setAttribute('preserveAspectRatio', 'none')
      svg.style.position = 'absolute'
      svg.style.left = '0'
      svg.style.top = '0'
      svg.style.overflow = 'visible'
      const path = document.createElementNS(ns, 'path')
      path.setAttribute('d', pointsToSvgPath(points, shape.fill_enabled, shape.smoothing))
      path.setAttribute('stroke', stroke)
      // Stroke width is in viewBox units, so divide by the (unknown) viewer
      // zoom indirectly via vector-effect: the line stays visually 2px wide
      // regardless of OSD's current zoom level.
      path.setAttribute('stroke-width', '2')
      path.setAttribute('vector-effect', 'non-scaling-stroke')
      path.setAttribute('fill', shape.fill_enabled ? fill : 'none')
      path.setAttribute('stroke-linecap', 'round')
      path.setAttribute('stroke-linejoin', 'round')
      svg.appendChild(path)
      root.appendChild(svg)
    }
  } else {
    // Default: rectangle.
    root = document.createElement('div')
    root.style.border = `2px dashed ${stroke}`
    root.style.background = fill
    root.style.pointerEvents = 'none'
    root.style.boxSizing = 'border-box'
  }

  if (!labelEl && !badgeEl) {
    return { root, labelRotator: null, labelEl: null, labelPosition, badgeEl: null }
  }

  const rotator = buildShapeLabelRotator()
  if (labelEl) rotator.appendChild(labelEl)
  if (badgeEl) rotator.appendChild(badgeEl)
  root.appendChild(rotator)
  return { root, labelRotator: rotator, labelEl, labelPosition, badgeEl }
}

// Pixel gap between the rotated AABB edge and the label/badge.
const SHAPE_LABEL_PAD = 4

// Update the label rotator and its children to track the shape's rotated
// axis-aligned bounding box in screen pixels. Called after render and on
// every viewport update (rotate / zoom) so labels stay horizontal at the
// correct screen-top / screen-bottom of the rotated shape.
export function applyShapeLabelTransforms(
  overlay: ShapeOverlay,
  viewportRotationDeg: number,
): void {
  const rotator = overlay.labelRotator
  if (!rotator) return
  const W = overlay.root.offsetWidth
  const H = overlay.root.offsetHeight
  // OSD may not have sized the overlay yet on first paint; bail and rely on
  // the next 'update-viewport' tick to re-run with valid dimensions.
  if (W === 0 && H === 0) return

  const theta = normalizeRotationDeg(viewportRotationDeg)
  rotator.style.transform = theta === 0 ? '' : `rotate(${-theta}deg)`

  const rad = (theta * Math.PI) / 180
  const c = Math.abs(Math.cos(rad))
  const s = Math.abs(Math.sin(rad))
  const hwRot = (W * c + H * s) / 2
  const hhRot = (W * s + H * c) / 2

  if (overlay.labelEl) {
    const el = overlay.labelEl
    if (overlay.labelPosition === 'below') {
      el.style.left = '0px'
      el.style.top = `${hhRot + SHAPE_LABEL_PAD}px`
      el.style.transform = 'translate(-50%, 0%)'
    } else if (overlay.labelPosition === 'center') {
      el.style.left = '0px'
      el.style.top = '0px'
      el.style.transform = 'translate(-50%, -50%)'
    } else {
      el.style.left = '0px'
      el.style.top = `${-(hhRot + SHAPE_LABEL_PAD)}px`
      el.style.transform = 'translate(-50%, -100%)'
    }
  }

  if (overlay.badgeEl) {
    const el = overlay.badgeEl
    el.style.left = `${-(hwRot + SHAPE_LABEL_PAD)}px`
    el.style.top = `${-(hhRot + SHAPE_LABEL_PAD)}px`
    el.style.transform = 'translate(-100%, -100%)'
  }
}

function pointsToSvgPath(points: ShapePoint[], closed: boolean, smooth: boolean): string {
  if (!points.length) return ''
  // Straight polyline: explicit opt-out, or too few points to smooth.
  if (!smooth || points.length < 3) return straightPath(points, closed)
  return smoothSvgPath(points, closed)
}

function straightPath(points: ShapePoint[], closed: boolean): string {
  const segs: string[] = [`M ${points[0].x} ${points[0].y}`]
  for (let i = 1; i < points.length; i++) {
    segs.push(`L ${points[i].x} ${points[i].y}`)
  }
  if (closed) segs.push('Z')
  return segs.join(' ')
}

// Smoothing tuned to erase the "staircase" a freehand stroke inherits when
// drawn at low magnification: there, one screen pixel spans many level-0
// pixels, so the pointer's integer-pixel path is stored as coarse, quantized
// steps. An *interpolating* spline (which threads every stored vertex) only
// rounds those steps' corners — the steps survive a zoom-in. So this instead
// LOW-PASSES the vertices (an approximating filter): the curve is pulled off
// the staircase toward the intended path, then a spline renders it cleanly. This
// does not pass exactly through the stored points, which is the point.
const SMOOTH_RADIUS = 2      // moving-average half-window (in resampled samples)
const SMOOTH_PASSES = 8      // iterations ≈ a Gaussian wide enough to cross steps
const MAX_SMOOTH_POINTS = 2000 // cap resample density so huge strokes stay cheap
// Floor on the resampled point count. The fixed-width moving average would
// otherwise span most of a *short* stroke — collapsing a small closed
// (fill_enabled) freehand blob to its centroid, or flattening a short open
// squiggle to a straight line between its endpoints. Upsampling short strokes
// keeps the kernel a small fraction of the curve, so smoothing degrades to
// gentle corner-rounding instead. Long low-zoom strokes are already denser
// than this, so their staircase removal is unaffected.
const MIN_SMOOTH_POINTS = 64

function smoothSvgPath(points: ShapePoint[], closed: boolean): string {
  // Estimate the draw-time quantization scale from the vertex spacing. It is
  // ~1 draw-screen-pixel, so it auto-scales the smoothing: strokes drawn at low
  // zoom (large spacing, bad staircase) get strong smoothing, strokes drawn at
  // high zoom (dense points, already clean) get light smoothing.
  const spacing = estimateSpacing(points)
  if (spacing <= 0) return straightPath(points, closed) // all-coincident/degenerate
  // Uniform arc-length resampling makes the low-pass isotropic along the curve
  // regardless of how unevenly the pointer sampling landed.
  const uniform = resampleUniform(points, spacing, closed)
  if (uniform.length < 3) return straightPath(points, closed)
  const smoothed = lowPass(uniform, SMOOTH_RADIUS, SMOOTH_PASSES, closed)
  return catmullRomPath(smoothed, closed)
}

// Median of the non-zero vertex gaps (robust to duplicate/coincident points).
function estimateSpacing(points: ShapePoint[]): number {
  const gaps: number[] = []
  for (let i = 1; i < points.length; i++) {
    const g = Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y)
    if (g > 0) gaps.push(g)
  }
  if (!gaps.length) return 0
  gaps.sort((a, b) => a - b)
  const mid = gaps.length >> 1
  return gaps.length % 2 ? gaps[mid] : (gaps[mid - 1] + gaps[mid]) / 2
}

// Resample the polyline at uniform arc-length `spacing`. Endpoints are kept for
// open strokes; closed strokes are walked as a loop.
function resampleUniform(points: ShapePoint[], spacing: number, closed: boolean): ShapePoint[] {
  const src = closed ? [...points, points[0]] : points
  const cum = [0]
  for (let i = 1; i < src.length; i++) {
    cum.push(cum[i - 1] + Math.hypot(src[i].x - src[i - 1].x, src[i].y - src[i - 1].y))
  }
  const total = cum[cum.length - 1]
  if (total <= 0) return points.slice()
  const steps = Math.min(MAX_SMOOTH_POINTS, Math.max(MIN_SMOOTH_POINTS, Math.round(total / spacing)))
  const out: ShapePoint[] = []
  let k = 0
  for (let s = 0; s <= steps; s++) {
    const target = (s / steps) * total
    while (k < cum.length - 2 && cum[k + 1] < target) k++
    const segLen = (cum[k + 1] - cum[k]) || 1
    const t = (target - cum[k]) / segLen
    out.push({ x: src[k].x + (src[k + 1].x - src[k].x) * t, y: src[k].y + (src[k + 1].y - src[k].y) * t })
  }
  // For a loop the last sample coincides with the first; drop it so the point
  // set stays a clean cycle for the wrapping low-pass / spline.
  if (closed) out.pop()
  return out
}

// Iterated moving average (≈ Gaussian). Open strokes hold their endpoints
// fixed so the annotation still starts/ends where it was drawn; closed strokes
// wrap around.
function lowPass(points: ShapePoint[], radius: number, passes: number, closed: boolean): ShapePoint[] {
  const n = points.length
  let cur = points.map((p) => ({ ...p }))
  const start = closed ? 0 : 1
  const end = closed ? n : n - 1
  for (let pass = 0; pass < passes; pass++) {
    const out = cur.map((p) => ({ ...p }))
    for (let i = start; i < end; i++) {
      let sx = 0, sy = 0
      for (let j = -radius; j <= radius; j++) {
        let idx = i + j
        idx = closed ? ((idx % n) + n) % n : Math.min(n - 1, Math.max(0, idx))
        sx += cur[idx].x
        sy += cur[idx].y
      }
      const c = 2 * radius + 1
      out[i] = { x: sx / c, y: sy / c }
    }
    cur = out
  }
  return cur
}

// Centripetal Catmull-Rom spline through `points`, emitted as cubic Béziers —
// the final clean render of the already-smoothed vertices. Centripetal
// parameterisation (alpha = 0.5) stays well-behaved (no overshoot / cusps).
function catmullRomPath(points: ShapePoint[], closed: boolean): string {
  const n = points.length
  // Neighbour lookup. Closed strokes wrap; open strokes reflect the endpoint
  // across its neighbour so the curve starts/ends with a natural tangent.
  // n >= 3 is guaranteed by the caller.
  const at = (i: number): ShapePoint => {
    if (closed) return points[((i % n) + n) % n]
    if (i < 0) return { x: 2 * points[0].x - points[1].x, y: 2 * points[0].y - points[1].y }
    if (i >= n) return { x: 2 * points[n - 1].x - points[n - 2].x, y: 2 * points[n - 1].y - points[n - 2].y }
    return points[i]
  }
  const segs: string[] = [`M ${points[0].x} ${points[0].y}`]
  const lastSeg = closed ? n : n - 1
  for (let i = 0; i < lastSeg; i++) {
    const [c1, c2] = catmullRomToBezier(at(i - 1), at(i), at(i + 1), at(i + 2))
    const p2 = at(i + 1)
    segs.push(`C ${c1.x} ${c1.y} ${c2.x} ${c2.y} ${p2.x} ${p2.y}`)
  }
  if (closed) segs.push('Z')
  return segs.join(' ')
}

const CR_ALPHA = 0.5

// Control points of the cubic Bézier matching the centripetal Catmull-Rom
// segment p1 -> p2 (p0, p3 are the outer neighbours). Knot spacings use
// distance^alpha; coincident points collapse to a straight segment to dodge
// division by zero.
function catmullRomToBezier(
  p0: ShapePoint, p1: ShapePoint, p2: ShapePoint, p3: ShapePoint,
): [ShapePoint, ShapePoint] {
  const EPS = 1e-6
  const t01 = Math.pow(Math.hypot(p1.x - p0.x, p1.y - p0.y), CR_ALPHA)
  const t12 = Math.pow(Math.hypot(p2.x - p1.x, p2.y - p1.y), CR_ALPHA)
  const t23 = Math.pow(Math.hypot(p3.x - p2.x, p3.y - p2.y), CR_ALPHA)
  if (t12 < EPS) return [{ x: p1.x, y: p1.y }, { x: p2.x, y: p2.y }]
  const s01 = Math.max(t01, EPS)
  const s23 = Math.max(t23, EPS)
  const m1x = p2.x - p1.x + t12 * ((p1.x - p0.x) / s01 - (p2.x - p0.x) / (s01 + t12))
  const m1y = p2.y - p1.y + t12 * ((p1.y - p0.y) / s01 - (p2.y - p0.y) / (s01 + t12))
  const m2x = p2.x - p1.x + t12 * ((p3.x - p2.x) / s23 - (p3.x - p1.x) / (s23 + t12))
  const m2y = p2.y - p1.y + t12 * ((p3.y - p2.y) / s23 - (p3.y - p1.y) / (s23 + t12))
  return [
    { x: p1.x + m1x / 3, y: p1.y + m1y / 3 },
    { x: p2.x - m2x / 3, y: p2.y - m2y / 3 },
  ]
}
