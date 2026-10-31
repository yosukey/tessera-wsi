import OpenSeadragon from 'openseadragon'
import type { PinShape } from './api'

export type PinColorName =
  | 'red'
  | 'orange'
  | 'yellow'
  | 'green'
  | 'blue'
  | 'purple'
  | 'white'
  | 'black'

export const PIN_COLORS: Record<PinColorName, string> = {
  red: '#ef4444',
  orange: '#f97316',
  yellow: '#eab308',
  green: '#22c55e',
  blue: '#3b82f6',
  purple: '#a855f7',
  white: '#ffffff',
  black: '#000000',
}

export const PIN_COLOR_ORDER: PinColorName[] = [
  'red', 'orange', 'yellow', 'green', 'blue', 'purple', 'white', 'black',
]

export const PIN_SHAPES: PinShape[] = ['pin', 'circle', 'arrow']

// Default pin shape for newly-created pins.
export const DEFAULT_PIN_SHAPE: PinShape = 'pin'

// CSS hex color literal (#RGB / #RGBA / #RRGGBB / #RRGGBBAA). The pin/shape
// SVG renderers interpolate the resolved color into innerHTML attributes
// (fill="${color}"), so anything that escapes this regex must NOT be
// returned — otherwise a stored value like `red"><script>...` would inject
// arbitrary markup. The named-palette branch returns a constant from the
// PIN_COLORS map and is therefore safe by construction.
const SAFE_HEX_COLOR = /^#[0-9a-fA-F]{3,8}$/

// Resolve a stored pin color (preset name OR hex string) into a CSS color.
// Mirrors resolveShapeColor so the pin palette can accept free-form hex
// values just like the shape palette does. Unknown / non-hex values fall
// back to the default palette color rather than being passed through, since
// callers feed the result directly into SVG markup.
export function resolvePinColor(name: string | undefined | null): string {
  if (!name) return PIN_COLORS.red
  // hasOwnProperty (not `in`): the latter also matches Object.prototype keys
  // like "constructor"/"toString", so a stored color of "constructor" would
  // resolve to a function and produce a garbage annotation.
  if (Object.prototype.hasOwnProperty.call(PIN_COLORS, name)) return PIN_COLORS[name as PinColorName]
  if (SAFE_HEX_COLOR.test(name)) return name
  return PIN_COLORS.red
}

// Pick a foreground (text) color that has acceptable contrast against the
// given background color. Used by the index-badge UI on both overlays and
// editor cards so the number stays readable on white, yellow, dark, etc.
export function contrastTextColor(cssColor: string): string {
  // Accept the same 3/4/6/8-digit hex forms that SAFE_HEX_COLOR (and thus
  // resolvePinColor) lets through; otherwise an alpha-bearing pin color like
  // #ffff0080 would fall through to a fixed white that is unreadable on a
  // light badge. The alpha channel is dropped — contrast is computed on RGB.
  const m = /^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(cssColor.trim())
  if (!m) return '#fff'
  let hex = m[1]
  if (hex.length === 3 || hex.length === 4) {
    hex = hex.slice(0, 3).split('').map((c) => c + c).join('')
  }
  const r = parseInt(hex.slice(0, 2), 16)
  const g = parseInt(hex.slice(2, 4), 16)
  const b = parseInt(hex.slice(4, 6), 16)
  const yiq = (r * 299 + g * 587 + b * 114) / 1000
  return yiq >= 160 ? '#111' : '#fff'
}

export function normalizePinShape(shape: string | undefined | null): PinShape {
  if (shape === 'arrow' || shape === 'pin' || shape === 'circle') return shape
  return DEFAULT_PIN_SHAPE
}

// Returns an OSD overlay placement that positions the pin's visual anchor
// (circle center, arrow tip, pin tip) exactly at the given image point.
export function pinPlacement(shape: PinShape) {
  return shape === 'arrow' || shape === 'pin'
    ? OpenSeadragon.Placement.BOTTOM
    : OpenSeadragon.Placement.CENTER
}

export function normalizeRotationDeg(raw: number | string | undefined | null): number {
  const n = typeof raw === 'number' ? raw : Number(raw)
  if (!Number.isFinite(n)) return 0
  const mod = ((n % 360) + 360) % 360
  return mod
}

// Small "#N" chip rendered alongside the pin/shape so the on-canvas item
// can be cross-referenced with the same-numbered card in the side editor.
// Uses the pin/shape color as background and an auto-contrast foreground.
export function buildIndexBadge(index: number, color: string): HTMLElement {
  const el = document.createElement('div')
  el.textContent = `#${index}`
  el.style.position = 'absolute'
  el.style.padding = '0 5px'
  el.style.minWidth = '16px'
  el.style.height = '16px'
  el.style.lineHeight = '16px'
  el.style.textAlign = 'center'
  el.style.borderRadius = '999px'
  el.style.background = color
  el.style.color = contrastTextColor(color)
  el.style.fontSize = '10px'
  el.style.fontWeight = '700'
  el.style.fontVariantNumeric = 'tabular-nums'
  el.style.whiteSpace = 'nowrap'
  el.style.pointerEvents = 'none'
  el.style.boxShadow = '0 0 0 1.5px rgba(255,255,255,0.95), 0 0 0 2.5px rgba(0,0,0,0.25)'
  return el
}

// Label is a SIBLING of the (optionally rotating) graphic so arrow rotation
// doesn't spin the text with it. Absolute positioning keeps the outer
// element's bounding box unchanged so OSD's placement anchor stays put.
// Caller is responsible for setting `left` / `top` / `transform`.
export function buildLabelElement(label: string): HTMLElement | null {
  const text = (label || '').trim()
  if (!text) return null
  const el = document.createElement('div')
  el.textContent = text
  el.style.position = 'absolute'
  el.style.padding = '2px 6px'
  el.style.borderRadius = '6px'
  el.style.background = 'rgba(17, 24, 39, 0.8)'
  el.style.color = '#fff'
  el.style.fontSize = '12px'
  el.style.fontWeight = '500'
  el.style.lineHeight = '1.2'
  el.style.whiteSpace = 'nowrap'
  el.style.pointerEvents = 'none'
  el.style.boxShadow = '0 0 0 1px rgba(0,0,0,0.15), 0 1px 2px rgba(0,0,0,0.25)'
  return el
}

// Arrow tail center in outer-box coordinates. The tip is at (11, 30)
// (bottom-center of the 22x30 box) and the tail center at 0° is (11, 0).
// Rotating that by θ clockwise around the tip gives the tail after rotation.
function arrowTailLocalPoint(rotationDeg: number): { x: number; y: number } {
  const rad = (rotationDeg * Math.PI) / 180
  return {
    x: 11 + 30 * Math.sin(rad),
    y: 30 * (1 - Math.cos(rad)),
  }
}

// Which edge of the label touches the tail, chosen per octant so the label
// always sits outside the rotated arrow body.
//   0°            → label's bottom-center on tail  (label above the tail)
//   180°          → label's top-center on tail     (label below the tail)
//   45/90/135°    → label's left-center on tail    (label to the right)
//   225/270/315°  → label's right-center on tail   (label to the left)
function arrowLabelTranslate(rotationDeg: number): string {
  const d = normalizeRotationDeg(rotationDeg)
  if (d === 0) return 'translate(-50%, -100%)'
  if (d === 180) return 'translate(-50%, 0%)'
  if (d > 0 && d < 180) return 'translate(0%, -50%)'
  return 'translate(-100%, -50%)'
}

// A pin overlay returned by createPinOverlay. `root` is what gets handed to
// OSD as the overlay element. `labelRotator`, when present, is a wrapper
// that holds the label and which is counter-rotated by the OSD viewport
// rotation so the label stays horizontal regardless of slide rotation.
// `graphicRotator`, when present, is the inner element whose `transform`
// must be updated on every viewport rotate so the graphic maintains its
// screen-space orientation: pins always tip-down, arrows always at their
// original placed angle.
export type PinOverlay = {
  root: HTMLElement
  labelRotator: HTMLElement | null
  graphicRotator: HTMLElement | null
  graphicBaseRotation: number
}

// A 0×0 wrapper positioned exactly at the pin's anchor (tip / center).
// The label inside is placed with coordinates *relative to the anchor*,
// so rotating the wrapper by -viewport_rot pivots around its own (0, 0) —
// which IS the anchor in outer-coordinates — keeping the label horizontal
// while preserving its screen position relative to the pin.
function buildLabelRotator(anchorXPx: number, anchorYPx: number): HTMLElement {
  const wrap = document.createElement('div')
  wrap.style.position = 'absolute'
  wrap.style.left = `${anchorXPx}px`
  wrap.style.top = `${anchorYPx}px`
  wrap.style.width = '0'
  wrap.style.height = '0'
  wrap.style.pointerEvents = 'none'
  return wrap
}

export function createPinOverlay(
  shape: PinShape,
  colorName: string | undefined | null,
  rotationDeg: number = 0,
  label: string = '',
  index: number | null = null,
): PinOverlay {
  const color = resolvePinColor(colorName)
  if (shape === 'arrow') {
    // Outer element is what OSD positions (OSD overwrites its `transform`
    // to apply placement offsets like translate(-50%, -100%) for BOTTOM).
    // The rotation lives on an inner child so OSD can't clobber it.
    const outer = document.createElement('div')
    outer.style.width = '22px'
    outer.style.height = '30px'
    outer.style.position = 'relative'
    outer.style.pointerEvents = 'none'

    const inner = document.createElement('div')
    inner.style.width = '22px'
    inner.style.height = '30px'
    const rot = normalizeRotationDeg(rotationDeg)
    // Always set transformOrigin so the pivot is correct when counter-rotation
    // is applied dynamically on viewport rotate. The tip is at bottom-center.
    inner.style.transformOrigin = '50% 100%'
    inner.style.transform = `rotate(${rot}deg)`
    // Arrow tip at the bottom-center (x=11, y=30) so BOTTOM placement anchors it
    // exactly on the pin location. A white halo keeps it visible on busy slides.
    inner.innerHTML = `
      <svg viewBox="0 0 22 30" width="22" height="30" xmlns="http://www.w3.org/2000/svg">
        <path d="M7 0 H15 V16 H20 L11 30 L2 16 H7 Z"
              fill="${color}"
              stroke="rgba(255,255,255,0.9)" stroke-width="1.5"
              stroke-linejoin="round"/>
      </svg>
    `
    outer.appendChild(inner)
    const labelEl = buildLabelElement(label)
    const badgeEl = index !== null ? buildIndexBadge(index, color) : null
    if (!labelEl && !badgeEl) return { root: outer, labelRotator: null, graphicRotator: inner, graphicBaseRotation: rot }
    // Wrap is anchored at the arrow tip (11, 30), so coords inside the wrap
    // are expressed relative to the tip rather than outer's top-left.
    const wrap = buildLabelRotator(11, 30)
    if (badgeEl) {
      // Float the chip upper-left of the tip, far enough to clear the arrow
      // body at the most common rotations (body extends 30px from tip).
      badgeEl.style.left = '0px'
      badgeEl.style.top = '0px'
      badgeEl.style.transform = 'translate(calc(-100% - 6px), calc(-100% - 30px))'
      wrap.appendChild(badgeEl)
    }
    if (labelEl) {
      const tail = arrowTailLocalPoint(rot)
      labelEl.style.left = `${tail.x - 11}px`
      labelEl.style.top = `${tail.y - 30}px`
      labelEl.style.transform = arrowLabelTranslate(rot)
      wrap.appendChild(labelEl)
    }
    outer.appendChild(wrap)
    return { root: outer, labelRotator: wrap, graphicRotator: inner, graphicBaseRotation: rot }
  }

  if (shape === 'pin') {
    // Map-marker style: teardrop body with a white inner dot. Tip at the
    // bottom-center (x=12, y=32) of a 24x32 box so BOTTOM placement anchors
    // the marker tip exactly on the pin location.
    const outer = document.createElement('div')
    outer.style.width = '24px'
    outer.style.height = '32px'
    outer.style.position = 'relative'
    outer.style.pointerEvents = 'none'
    // SVG lives in an inner wrapper so the graphic can be counter-rotated
    // around the tip (12, 32) without conflicting with OSD's positioning
    // transform on outer.
    const inner = document.createElement('div')
    inner.style.width = '24px'
    inner.style.height = '32px'
    inner.style.transformOrigin = '12px 32px'
    inner.innerHTML = `
      <svg viewBox="0 0 24 32" width="24" height="32" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 1 C5.92 1 1 5.92 1 12 C1 20.5 12 31 12 31 C12 31 23 20.5 23 12 C23 5.92 18.08 1 12 1 Z"
              fill="${color}"
              stroke="rgba(255,255,255,0.95)" stroke-width="1.5"
              stroke-linejoin="round"/>
        <circle cx="12" cy="12" r="4" fill="rgba(255,255,255,0.95)"/>
      </svg>
    `
    outer.appendChild(inner)
    const labelEl = buildLabelElement(label)
    const badgeEl = index !== null ? buildIndexBadge(index, color) : null
    if (!labelEl && !badgeEl) return { root: outer, labelRotator: null, graphicRotator: inner, graphicBaseRotation: 0 }
    // Wrap is anchored at the marker tip (12, 32). Place the label so its
    // left edge sits just past the head's right side (head right ≈ x=23 in
    // outer-coords ⇒ +11 from tip x), vertically centered on the head
    // center (y=12 in outer-coords ⇒ -20 from tip y).
    const wrap = buildLabelRotator(12, 32)
    if (badgeEl) {
      // Float the chip just upper-left of the head (head left edge is at
      // outer x=1 ⇒ -11 from tip; head top is around y=1 ⇒ -31 from tip).
      badgeEl.style.left = '-11px'
      badgeEl.style.top = '-31px'
      badgeEl.style.transform = 'translate(-100%, -100%)'
      wrap.appendChild(badgeEl)
    }
    if (labelEl) {
      labelEl.style.left = '11px'
      labelEl.style.top = '-20px'
      labelEl.style.transform = 'translate(2px, -50%)'
      wrap.appendChild(labelEl)
    }
    outer.appendChild(wrap)
    return { root: outer, labelRotator: wrap, graphicRotator: inner, graphicBaseRotation: 0 }
  }

  // Default: filled circle dot.
  const outer = document.createElement('div')
  outer.style.width = '14px'
  outer.style.height = '14px'
  outer.style.position = 'relative'
  outer.style.pointerEvents = 'none'

  const dot = document.createElement('div')
  dot.style.width = '14px'
  dot.style.height = '14px'
  dot.style.borderRadius = '50%'
  dot.style.background = color
  dot.style.boxShadow = '0 0 0 2px rgba(255,255,255,0.85)'
  outer.appendChild(dot)
  const labelEl = buildLabelElement(label)
  const badgeEl = index !== null ? buildIndexBadge(index, color) : null
  if (!labelEl && !badgeEl) return { root: outer, labelRotator: null, graphicRotator: null, graphicBaseRotation: 0 }
  // Wrap is anchored at the dot's center (7, 7). Label sits just to the
  // right of the dot's edge (dot radius = 7), vertically centered.
  const wrap = buildLabelRotator(7, 7)
  if (badgeEl) {
    // Float the chip just upper-left of the dot (dot left edge at -7,
    // top at -7 from center anchor).
    badgeEl.style.left = '-7px'
    badgeEl.style.top = '-7px'
    badgeEl.style.transform = 'translate(-100%, -100%)'
    wrap.appendChild(badgeEl)
  }
  if (labelEl) {
    labelEl.style.left = '7px'
    labelEl.style.top = '0px'
    labelEl.style.transform = 'translate(4px, -50%)'
    wrap.appendChild(labelEl)
  }
  outer.appendChild(wrap)
  return { root: outer, labelRotator: wrap, graphicRotator: null, graphicBaseRotation: 0 }
}

// Apply counter-rotation to a pin/arrow graphic element so it maintains its
// original screen-space orientation when the viewport rotates.
// baseRotation: the graphic's intended screen angle (0 for pin, rotation_deg for arrow).
// The effective transform becomes rotate(baseRotation - viewportRotationDeg), cancelling
// the viewport rotation while preserving the originally placed angle.
export function applyGraphicCounterRotation(
  el: HTMLElement,
  baseRotation: number,
  viewportRotationDeg: number,
): void {
  el.style.transform = `rotate(${baseRotation - viewportRotationDeg}deg)`
}

// Apply the inverse of the current viewport rotation to a label rotator
// so the label text stays horizontal even when the slide is rotated.
export function applyLabelCounterRotation(rotator: HTMLElement, viewportRotationDeg: number): void {
  const r = normalizeRotationDeg(viewportRotationDeg)
  if (r === 0) {
    rotator.style.transform = ''
  } else {
    rotator.style.transform = `rotate(${-r}deg)`
  }
}
