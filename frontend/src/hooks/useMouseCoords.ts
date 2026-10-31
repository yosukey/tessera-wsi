import { useEffect, useState, type RefObject } from 'react'
import type OpenSeadragon from 'openseadragon'
import { attachMouseCoordsTracker } from '../lib/osd'

/**
 * Image-coordinate position of the pointer over an OSD viewer, tracked only
 * while ``enabled``.
 *
 * Every pointer move would otherwise set React state and re-render the whole
 * owner (the ~1100-line tour editor), yet the coordinates are shown only in
 * debug / pin / shape modes. ``generation`` is a counter the owner bumps when
 * the viewer opens, so the tracker re-attaches to a rebuilt viewer.
 */
export function useMouseCoords(
  viewerRef: RefObject<OpenSeadragon.Viewer | null>,
  enabled: boolean,
  generation: number,
): { x: number; y: number } | null {
  const [coords, setCoords] = useState<{ x: number; y: number } | null>(null)
  useEffect(() => {
    const v = viewerRef.current
    if (!enabled || !v) {
      setCoords(null)
      return
    }
    const detach = attachMouseCoordsTracker(v, setCoords)
    return () => {
      // The viewer may already be destroyed by its own effect's cleanup.
      try { detach() } catch { /* swallow */ }
      setCoords(null)
    }
  }, [viewerRef, enabled, generation])
  return coords
}
