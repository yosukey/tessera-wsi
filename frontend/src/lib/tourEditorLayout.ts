import type { CSSProperties } from 'react'

// A tablet held upright (iPads are 744–1032px wide in portrait, up to ~1192px
// on a large iPad Pro with Display Zoom set to More Space), or a window shaped
// like one. The tour pages stack the slide above the step panel there too, so
// rotating the device switches the layout: side by side, a portrait iPad
// leaves the slide a tall strip (TourViewerPage) or a quarter of the width
// (the editor's fixed 560px sidebar). The width cap keeps wide portrait
// monitors, which have room for both, side by side.
export const PORTRAIT_TABLET_QUERY = '(orientation: portrait) and (max-width: 1200px)'

// TourViewerPage's step panel stacks under the slide below 720px (the panel
// alone would be wider than a phone held upright) and on a portrait tablet.
export const TOUR_VIEWER_STACK_QUERY = `(max-width: 720px), ${PORTRAIT_TABLET_QUERY}`

// The fixed 560px sidebar leaves the slide ~240px at 800px, so below it the
// editor stacks the slide above the sidebar instead, as it does on a portrait
// tablet (cf. TourViewerPage's 720px stacked mode, whose sidebar is narrower).
export const TOUR_EDITOR_STACK_QUERY = `(max-width: 800px), ${PORTRAIT_TABLET_QUERY}`

/**
 * Layout of the tour editor's three boxes: the page container, the slide pane
 * and the step sidebar. Side by side on a wide screen; stacked (slide on top,
 * sidebar below) on a narrow one. ``minWidth/minHeight: 0`` lets the slide pane
 * shrink inside the flex row/column instead of being propped open by its
 * content (without which the editor would be unusable below ~700px).
 */
export function tourEditorLayout(stacked: boolean, height: string): {
  container: CSSProperties
  main: CSSProperties
  aside: CSSProperties
} {
  return {
    container: { display: 'flex', flexDirection: stacked ? 'column' : 'row', height },
    main: { flex: 1, position: 'relative', minWidth: 0, minHeight: 0 },
    aside: stacked
      ? {
          flex: '0 0 55%',
          minHeight: 0,
          borderTop: '1px solid var(--color-border)',
          display: 'flex',
          flexDirection: 'column',
          background: 'var(--color-surface)',
        }
      : {
          width: 560,
          flexShrink: 0,
          borderLeft: '1px solid var(--color-border)',
          display: 'flex',
          flexDirection: 'column',
          background: 'var(--color-surface)',
        },
  }
}
