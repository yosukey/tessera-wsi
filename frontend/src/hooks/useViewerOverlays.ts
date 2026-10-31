import { useEffect, useRef, type RefObject } from 'react'
import type OpenSeadragon from 'openseadragon'
import type { Slide } from '../lib/api'
import { useI18n } from '../lib/i18n'
import { attachScaleBar, slidePixelSize } from '../lib/scalebar'
import { attachZoomIndicator } from '../lib/zoomIndicator'
import { attachDigitalZoomBadge } from '../lib/digitalZoomBadge'

/**
 * The viewport overlays the viewer and the tour viewer draw over a slide: the
 * scale bar, the magnification indicator and the digital-zoom badge. Returns
 * the refs of their container elements; the page renders each container
 * (the first two only while shown).
 *
 * ``osdGen`` is the counter the page bumps on every viewer (re)open. The
 * viewer is built in an async effect, so an overlay effect keyed only on the
 * slide can run before ``viewerRef`` is set, bail, and never re-attach;
 * keying on the counter re-attaches each overlay to the viewport that has
 * just opened.
 */
export function useViewerOverlays({ viewerRef, slide, osdGen, showScaleBar, showZoomIndicator }: {
  viewerRef: RefObject<OpenSeadragon.Viewer | null>
  slide: Slide | null
  osdGen: number
  showScaleBar: boolean
  showZoomIndicator: boolean
}) {
  const { t } = useI18n()
  const scaleBarDivRef = useRef<HTMLDivElement | null>(null)
  const zoomIndicatorDivRef = useRef<HTMLDivElement | null>(null)
  const digitalZoomBadgeDivRef = useRef<HTMLDivElement | null>(null)
  const slideKey = slide?.public_id

  // Scale bar. Per-axis MPP, so a rotated slide with non-square pixels is
  // measured along the axis the bar actually spans (see lib/scalebar).
  useEffect(() => {
    if (!showScaleBar) return
    const viewer = viewerRef.current
    if (!slide || !viewer || !scaleBarDivRef.current) return
    const mpp = slidePixelSize(slide)
    if (!mpp) return
    const detach = attachScaleBar({ viewer, mpp, container: scaleBarDivRef.current })
    return () => { try { detach() } catch {} }
  }, [slide?.mpp_x, slide?.mpp_y, slideKey, showScaleBar, osdGen])

  // Magnification indicator — only with a known MPP, which it is calibrated
  // against.
  useEffect(() => {
    if (!showZoomIndicator) return
    const viewer = viewerRef.current
    if (!slide || !viewer || !zoomIndicatorDivRef.current) return
    const mpp = slide.mpp_x || slide.mpp_y || 0
    if (!mpp) return
    const detach = attachZoomIndicator({ viewer, mpp, container: zoomIndicatorDivRef.current })
    return () => { try { detach() } catch {} }
  }, [slide?.mpp_x, slide?.mpp_y, slideKey, showZoomIndicator, osdGen])

  // Digital-zoom badge — whenever the viewport is zoomed past the image's
  // native (1:1) resolution. Independent of MPP and of the indicator setting,
  // so viewers always see the warning.
  useEffect(() => {
    const viewer = viewerRef.current
    if (!slide || !viewer || !digitalZoomBadgeDivRef.current) return
    const detach = attachDigitalZoomBadge({
      viewer,
      container: digitalZoomBadgeDivRef.current,
      label: t('viewer.digitalZoom'),
    })
    return () => { try { detach() } catch {} }
  }, [slideKey, slide?.id, osdGen, t])

  return { scaleBarDivRef, zoomIndicatorDivRef, digitalZoomBadgeDivRef }
}
