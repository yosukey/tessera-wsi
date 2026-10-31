import OpenSeadragon from 'openseadragon'

// Physical size of one level-0 pixel along each image axis, in µm. OME-TIFF
// (PhysicalSizeX / PhysicalSizeY) and the admin MPP editor carry the two axes
// independently, so a pixel need not be square.
export type PixelSize = { x: number; y: number }

// Per-axis MPP for a slide, or null when neither axis is calibrated. A slide
// with only one axis recorded is treated as square-pixelled at that value —
// the same fallback as a single-factor `mpp_x || mpp_y`.
export function slidePixelSize(slide: { mpp_x?: number | null; mpp_y?: number | null }): PixelSize | null {
  const x = slide.mpp_x && slide.mpp_x > 0 && Number.isFinite(slide.mpp_x) ? slide.mpp_x : 0
  const y = slide.mpp_y && slide.mpp_y > 0 && Number.isFinite(slide.mpp_y) ? slide.mpp_y : 0
  if (!x && !y) return null
  return { x: x || y, y: y || x }
}

type ScaleBarOptions = {
  viewer: OpenSeadragon.Viewer
  // µm per level-0 pixel, positive and finite on both axes (callers bail when
  // slidePixelSize returns null). A bare number means square pixels; pass a
  // PixelSize (see slidePixelSize) so an anisotropic slide measures correctly
  // at any rotation.
  mpp: number | PixelSize
  container: HTMLDivElement
}

// "Nice" scale-bar lengths are 1/2/5 × 10ⁿ µm, so three significant digits
// always represent them exactly while absorbing float noise (2 * 1e-4 etc.).
// Fixed en-US so the decimal separator matches the rest of the overlay, and
// no grouping so 1000+ never renders as "1,000".
const LENGTH_FORMAT = new Intl.NumberFormat('en-US', { maximumSignificantDigits: 3, useGrouping: false })

// Label for a positive length in µm. Switches to nm below 1 µm and mm from
// 1000 µm, and formats by significant digits rather than rounding to an
// integer, so a sub-µm bar (high-resolution scans, deep digital zoom) can
// never read "0 µm" while the bar itself shows a real length.
export function formatLengthUm(um: number): string {
  if (um >= 1000) return `${LENGTH_FORMAT.format(um / 1000)} mm`
  if (um >= 1) return `${LENGTH_FORMAT.format(um)} µm`
  return `${LENGTH_FORMAT.format(um * 1000)} nm`
}

// Largest 'nice' physical length (1/2/5 × 10ⁿ) whose on-screen length fits
// within maxPx. Picking the largest value that *fits* — rather than the
// smallest that exceeds a fractional target — guarantees
// lenUm / umPerPx <= maxPx, so the bar drawn matches the label exactly, with
// no post-hoc clamp that would make the label describe a length longer than
// the bar actually rendered. The 1-2-5 sequence never skips more than 2.5×,
// so the chosen bar is always between maxPx / 2.5 and maxPx long, at any
// scale. Caller guarantees umPerPx > 0 and finite.
export function niceLength(umPerPx: number, maxPx: number): number {
  const maxUm = umPerPx * maxPx
  const bases = [1, 2, 5]
  const exp = Math.floor(Math.log10(maxUm))
  let best = Math.pow(10, exp - 3)
  for (let e = exp - 3; e <= exp + 3; e++) {
    for (const b of bases) {
      const cand = b * Math.pow(10, e)
      if (cand <= maxUm) best = cand
    }
  }
  return best
}

/**
 * Lightweight scalebar that works without external plugins.
 * It estimates current micrometers-per-screen-pixel by converting two screen points into image coordinates.
 */
export function attachScaleBar(opts: ScaleBarOptions) {
  const { viewer, container } = opts
  const px: PixelSize = typeof opts.mpp === 'number' ? { x: opts.mpp, y: opts.mpp } : opts.mpp

  container.style.position = 'absolute'
  // Offset right of the image-adjust toggle button (~32px wide at left:12px)
  // so the two never overlap, regardless of whether the toggle is mounted.
  container.style.left = 'calc(56px + env(safe-area-inset-left, 0px))'
  container.style.bottom = 'calc(12px + env(safe-area-inset-bottom, 0px))'
  container.style.zIndex = '20'
  container.style.pointerEvents = 'none'
  container.style.fontSize = '12px'
  container.style.color = 'rgba(0,0,0,0.85)'
  container.style.background = 'rgba(255,255,255,0.82)'
  container.style.borderRadius = '5px'
  container.style.padding = '4px 8px 6px'

  const label = document.createElement('div')
  label.style.marginBottom = '4px'
  label.style.opacity = '0.85'
  const barWrap = document.createElement('div')
  barWrap.style.height = '6px'
  barWrap.style.borderRadius = '4px'
  barWrap.style.overflow = 'hidden'

  const bar = document.createElement('div')
  bar.style.height = '100%'
  bar.style.background = 'rgba(0,0,0,0.75)'
  bar.style.width = '80px'
  bar.style.borderRadius = '4px'
  barWrap.appendChild(bar)

  container.appendChild(label)
  container.appendChild(barWrap)

  function update() {
    const p1 = new OpenSeadragon.Point(20, 20)
    const p2 = new OpenSeadragon.Point(120, 20)
    const vp1 = viewer.viewport.pointFromPixel(p1, true)
    const vp2 = viewer.viewport.pointFromPixel(p2, true)
    const img1 = viewer.viewport.viewportToImageCoordinates(vp1)
    const img2 = viewer.viewport.viewportToImageCoordinates(vp2)

    // Convert the image-space delta per axis before taking its length. The
    // bar is screen-horizontal, which after a rotation runs along image Y (or
    // obliquely), so scaling one Euclidean pixel distance by a single MPP
    // mislabels anisotropic slides — e.g. at 90° a bar spanning 0.5 µm/px Y
    // pixels would be labelled with the 0.25 µm/px X factor, reading half its
    // real length. Identical to the single-factor formula when x == y.
    const dUm = Math.hypot((img2.x - img1.x) * px.x, (img2.y - img1.y) * px.y)
    const screenD = Math.hypot(p2.x - p1.x, p2.y - p1.y)
    const umPerScreenPx = dUm / screenD
    // A degenerate viewport (zero-size container mid-layout) yields 0 or a
    // non-finite value; keep the last good bar rather than drawing nonsense.
    if (!(umPerScreenPx > 0) || !Number.isFinite(umPerScreenPx)) return

    const maxPx = 140
    const lenUm = niceLength(umPerScreenPx, maxPx)
    const lenPx = Math.min(maxPx, lenUm / umPerScreenPx)

    bar.style.width = `${lenPx}px`
    barWrap.style.width = `${lenPx}px`
    label.textContent = formatLengthUm(lenUm)
  }

  // rAF-coalesce so a single frame's worth of zoom/pan/rotate events
  // produces one DOM write rather than 3-5. Matches the pattern already
  // used in TourViewerPage's overlay-rotation handler.
  let rafId = 0
  const handler = () => {
    if (rafId) return
    rafId = requestAnimationFrame(() => { rafId = 0; update() })
  }
  // OSD raises 'zoom'/'pan'/'rotate' only once, when a new target is *set* —
  // not per animation frame. So for a spring-animated move (e.g. a tour step's
  // smooth transition, which sets the target once via fitBounds) those events
  // fire before the viewport has moved, and update() reads the still-old
  // position. 'animation' fires every frame so the bar glides with the move,
  // and 'animation-finish' guarantees a final recompute once the springs
  // settle. Keep the target-set events too, so an immediate (non-animated)
  // jump — which raises no 'animation' — still refreshes the bar.
  viewer.addHandler('zoom', handler)
  viewer.addHandler('pan', handler)
  viewer.addHandler('rotate', handler)
  viewer.addHandler('animation', handler)
  viewer.addHandler('animation-finish', handler)
  viewer.addHandler('open', handler)
  viewer.addHandler('resize', handler)

  update()

  return () => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0 }
    try {
      viewer.removeHandler('zoom', handler)
      viewer.removeHandler('pan', handler)
      viewer.removeHandler('rotate', handler)
      viewer.removeHandler('animation', handler)
      viewer.removeHandler('animation-finish', handler)
      viewer.removeHandler('open', handler)
      viewer.removeHandler('resize', handler)
    } catch {}
    try { container.removeChild(label) } catch {}
    try { container.removeChild(barWrap) } catch {}
    // The container is caller-owned and was styled into a visible white pill on
    // attach (background / padding / radius). Removing only the children leaves
    // that empty pill painted in the bottom-left corner, so reset the visual
    // styles this attach applied to make the scale bar fully disappear.
    container.style.background = ''
    container.style.padding = ''
    container.style.borderRadius = ''
  }
}
