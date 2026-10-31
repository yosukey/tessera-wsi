// Minimal OpenSeadragon viewer model for overlay tests: image coordinates are
// viewport coordinates, one screen pixel spans `imgPxPerScreenPx` image
// pixels, and the view is rotated by `rotDeg` about the screen origin.
import OpenSeadragon from 'openseadragon'

export function fakeViewer(rotDeg: number, imgPxPerScreenPx = 1) {
  const handlers = new Map<string, Set<(...a: unknown[]) => void>>()
  const t = (rotDeg * Math.PI) / 180
  const viewport = {
    pointFromPixel(p: { x: number; y: number }) {
      // screen = R(rot) · image / scale  ⇒  image = R(−rot) · screen · scale
      const x = p.x * imgPxPerScreenPx
      const y = p.y * imgPxPerScreenPx
      return new OpenSeadragon.Point(x * Math.cos(t) + y * Math.sin(t), -x * Math.sin(t) + y * Math.cos(t))
    },
    viewportToImageCoordinates(p: OpenSeadragon.Point) {
      return p
    },
  }
  return {
    viewport,
    addHandler(name: string, fn: (...a: unknown[]) => void) {
      if (!handlers.has(name)) handlers.set(name, new Set())
      handlers.get(name)!.add(fn)
    },
    removeHandler(name: string, fn: (...a: unknown[]) => void) {
      handlers.get(name)?.delete(fn)
    },
    handlerCount() {
      let n = 0
      for (const s of handlers.values()) n += s.size
      return n
    },
  }
}
