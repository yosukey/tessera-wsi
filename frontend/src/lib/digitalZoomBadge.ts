import OpenSeadragon from 'openseadragon'

type Options = {
  viewer: OpenSeadragon.Viewer
  container: HTMLDivElement
  label?: string
}

// Shows a small warning badge whenever the viewport is zoomed past the image's
// native resolution (1 screen pixel > 1 image pixel at level 0). At that point
// any further zoom is purely digital — pixels get interpolated and no real
// detail is added. Compares screen↔image pixel ratio directly via OSD's
// viewportToImageZoom, so it works even when MPP is unknown.
export function attachDigitalZoomBadge({ viewer, container, label = 'Digital Zoom' }: Options) {
  container.innerHTML = ''

  Object.assign(container.style, {
    position: 'absolute',
    top: '56px',
    left: '12px',
    zIndex: '10',
    pointerEvents: 'none',
    userSelect: 'none',
    display: 'none',
  })

  const badge = document.createElement('div')
  Object.assign(badge.style, {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '5px',
    padding: '4px 9px',
    background: 'rgba(180, 60, 40, 0.85)',
    color: '#fff',
    fontSize: '11px',
    fontWeight: '700',
    letterSpacing: '0.04em',
    borderRadius: '999px',
    border: '1px solid rgba(255,255,255,0.35)',
    boxShadow: '0 1px 4px rgba(0,0,0,0.45)',
    textShadow: '0 0 3px rgba(0,0,0,0.6)',
    whiteSpace: 'nowrap',
  })

  const dot = document.createElement('span')
  Object.assign(dot.style, {
    width: '6px',
    height: '6px',
    borderRadius: '50%',
    background: 'rgba(255, 220, 60, 0.95)',
    boxShadow: '0 0 4px rgba(255, 220, 60, 0.8)',
    flexShrink: '0',
  })
  badge.appendChild(dot)

  const text = document.createElement('span')
  text.textContent = label
  badge.appendChild(text)

  container.appendChild(badge)

  // Small tolerance over native (1:1). At a maxZoomPixelRatio of 1.0 the zoom
  // clamp parks the view exactly at native, but the round-trip through
  // viewportToImageZoom can land a hair above 1.0 on float rounding — without
  // this slack the badge falsely flickers on at the cap, reading as "digital
  // zoom" when the admin set the cap precisely to disable it. 0.1% is far
  // below any perceptible interpolation, so a genuinely digital view (e.g. a
  // 1.1 cap) still trips it.
  const NATIVE_EPSILON = 1e-3
  function isDigital(): boolean {
    try {
      const vpZoom = viewer.viewport.getZoom(true)
      const imgZoom = viewer.viewport.viewportToImageZoom(vpZoom)
      return isFinite(imgZoom) && imgZoom > 1.0 + NATIVE_EPSILON
    } catch {
      return false
    }
  }

  function update() {
    container.style.display = isDigital() ? 'block' : 'none'
  }

  // rAF-coalesce so a burst of zoom events doesn't toggle display style
  // (and trigger style recalc) multiple times per frame.
  let rafId = 0
  const h = () => {
    if (rafId) return
    rafId = requestAnimationFrame(() => { rafId = 0; update() })
  }
  // 'zoom' fires only when a zoom target is set, not per animation frame, so a
  // spring-animated move (e.g. a tour step transition) would evaluate isDigital()
  // against the pre-animation zoom. 'animation' tracks the glide and
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
    // The update handler toggles container.style.display to show/hide the
    // badge; nothing resets it on teardown. Detaching while the badge is
    // visible would otherwise leave a stale "Digital Zoom" pill painted over
    // the viewer — clear the node and re-hide the caller-owned container.
    try { container.innerHTML = '' } catch {}
    container.style.display = 'none'
  }
}
