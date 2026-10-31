import OpenSeadragon from 'openseadragon'

type Options = {
  viewer: OpenSeadragon.Viewer
  mpp: number // micrometers per pixel at level 0; caller must guarantee > 0
  container: HTMLDivElement
}

const TRACK_HEIGHT = 160

const MAG_TICKS = [1, 2, 5, 10, 20, 40, 100]
const MAG_MIN = 0.7
const MAG_MAX = 150

function logFrac(val: number, min: number, max: number): number {
  return (Math.log10(val) - Math.log10(min)) / (Math.log10(max) - Math.log10(min))
}

export function attachZoomIndicator({ viewer, mpp, container }: Options) {
  const ticks = MAG_TICKS
  const valMin = MAG_MIN
  const valMax = MAG_MAX

  container.innerHTML = ''

  Object.assign(container.style, {
    position: 'absolute',
    top: '196px',
    left: '44px',
    zIndex: '10',
    pointerEvents: 'none',
    userSelect: 'none',
  })

  const track = document.createElement('div')
  Object.assign(track.style, {
    position: 'relative',
    width: '55px',
    height: `${TRACK_HEIGHT}px`,
  })
  container.appendChild(track)

  // Vertical scale line
  const scaleLine = document.createElement('div')
  Object.assign(scaleLine.style, {
    position: 'absolute',
    left: '0',
    top: '0',
    bottom: '0',
    width: '2px',
    background: 'rgba(255,255,255,0.28)',
    borderRadius: '1px',
  })
  track.appendChild(scaleLine)

  // Static tick marks + labels
  for (const val of ticks) {
    const frac = logFrac(val, valMin, valMax)
    const topPx = (1 - frac) * TRACK_HEIGHT

    const tickMark = document.createElement('div')
    Object.assign(tickMark.style, {
      position: 'absolute',
      left: '2px',
      top: `${topPx}px`,
      width: '5px',
      height: '1px',
      background: 'rgba(255,255,255,0.28)',
      transform: 'translateY(-50%)',
    })
    track.appendChild(tickMark)

    const label = document.createElement('span')
    label.textContent = `${val}×`
    Object.assign(label.style, {
      position: 'absolute',
      left: '9px',
      top: `${topPx}px`,
      transform: 'translateY(-50%)',
      fontSize: '9px',
      color: 'rgba(255,255,255,0.42)',
      textShadow: '0 0 3px rgba(0,0,0,0.9)',
      whiteSpace: 'nowrap',
      lineHeight: '1',
    })
    track.appendChild(label)
  }

  // Moving pointer: ◄ arrowhead + current value label
  // pointer.left = 2px so the ◄ tip touches the right edge of the scale line
  const pointer = document.createElement('div')
  Object.assign(pointer.style, {
    position: 'absolute',
    left: '2px',
    top: '50%',
    display: 'flex',
    alignItems: 'center',
    transform: 'translateY(-50%)',
    transition: 'top 0.1s ease-out',
    zIndex: '1',
  })
  track.appendChild(pointer)

  // ◄  (border-right → triangle pointing left)
  const arrowHead = document.createElement('div')
  Object.assign(arrowHead.style, {
    width: '0',
    height: '0',
    borderTop: '5px solid transparent',
    borderBottom: '5px solid transparent',
    borderRight: '7px solid rgba(255,220,60,0.95)',
    flexShrink: '0',
    filter: 'drop-shadow(0 0 2px rgba(0,0,0,0.7))',
  })
  pointer.appendChild(arrowHead)

  const valueLabel = document.createElement('span')
  Object.assign(valueLabel.style, {
    fontSize: '11px',
    fontWeight: '700',
    color: 'rgba(255,220,60,0.95)',
    textShadow: '0 0 4px rgba(0,0,0,0.9)',
    marginLeft: '3px',
    lineHeight: '1',
    whiteSpace: 'nowrap',
  })
  pointer.appendChild(valueLabel)

  function getVal(): number | null {
    try {
      const vpZoom = viewer.viewport.getZoom(true)
      const imgZoom = viewer.viewport.viewportToImageZoom(vpZoom)
      return 10 * imgZoom / mpp
    } catch {
      return null
    }
  }

  function fmt(val: number): string {
    if (val >= 10) return `${Math.round(val)}×`
    if (val >= 1) return `${val.toFixed(1)}×`
    return `${val.toFixed(2)}×`
  }

  function update() {
    const val = getVal()
    if (val == null || val <= 0 || !isFinite(val)) return
    const clamped = Math.max(valMin, Math.min(valMax, val))
    const frac = logFrac(clamped, valMin, valMax)
    pointer.style.top = `${(1 - frac) * TRACK_HEIGHT}px`
    valueLabel.textContent = fmt(val)
  }

  // rAF-coalesce so rapid zoom event bursts (pinch / wheel inertia)
  // collapse into one DOM write per frame.
  let rafId = 0
  const h = () => {
    if (rafId) return
    rafId = requestAnimationFrame(() => { rafId = 0; update() })
  }
  // 'zoom' fires only when a new zoom target is set, not per animation frame,
  // so a spring-animated move (e.g. a tour step transition) would leave the
  // indicator on the pre-animation value. 'animation' tracks the glide and
  // 'animation-finish' guarantees a final settle; 'zoom' still covers an
  // immediate (non-animated) jump that raises no 'animation'.
  viewer.addHandler('zoom', h)
  viewer.addHandler('animation', h)
  viewer.addHandler('animation-finish', h)
  viewer.addHandler('open', h)
  viewer.addHandler('resize', h)
  update()

  return () => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0 }
    try {
      viewer.removeHandler('zoom', h)
      viewer.removeHandler('animation', h)
      viewer.removeHandler('animation-finish', h)
      viewer.removeHandler('open', h)
      viewer.removeHandler('resize', h)
    } catch {}
    // Remove the appended node too. The next attach() clears via
    // innerHTML='', but a detach with no following re-attach (indicator
    // toggled off, viewer destroyed) would otherwise leave the track
    // painted in the container — mirrors scalebar.ts's detacher.
    try { container.removeChild(track) } catch {}
  }
}
