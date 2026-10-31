import React, { useState } from 'react'
import type { Slide, TourSummary } from '../../lib/api'
import SlideRow from './SlideRow'
import TourRow from './TourRow'
import TourExpander from './TourExpander'

type Props = {
  slide: Slide
  // Tours belonging to this slide. Empty unless the page is in the
  // "All (slides & tours)" view.
  tours: TourSummary[]
  thumbWidth: number
  thumbHeight: number
  cardAttrs: string[]
  // Admin-configured cap on how many tours are listed before the rest
  // collapse behind the expander (viewer_settings.index_tours_per_slide).
  // 0 is a valid value — list none until the user asks.
  toursPerSlide: number
}

/**
 * One slide in the list view, plus its inline tour rows.
 *
 * Uncapped, a slide carrying twenty tours would produce twenty ~39px rows
 * under a 162px slide row — five screens of one slide, with every other slide
 * pushed below the fold. The cap and its expander mirror the tile card
 * (SlideCard), so both views agree on how much vertical space one slide may
 * claim; the count badge on the slide row itself still reports the true total.
 */
export default function SlideListItem({
  slide: s,
  tours: slideTours,
  thumbWidth,
  thumbHeight,
  cardAttrs,
  toursPerSlide,
}: Props) {
  const [expanded, setExpanded] = useState(false)
  const visibleTours = expanded ? slideTours : slideTours.slice(0, toursPerSlide)
  const hiddenCount = slideTours.length - visibleTours.length
  // Gate on the cap rather than on hiddenCount so the control survives its own
  // click and can collapse the list again (see SlideCard for the same note).
  const canToggle = slideTours.length > toursPerSlide

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <SlideRow
        slide={s}
        tourCount={slideTours.length}
        thumbWidth={thumbWidth}
        thumbHeight={thumbHeight}
        cardAttrs={cardAttrs}
      />
      {slideTours.length > 0 && (
        <div style={{
          borderLeft: '2px solid var(--color-border)',
          marginLeft: 24,
          paddingLeft: 12,
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
        }}>
          {visibleTours.map(tour => (
            <TourRow key={tour.id} tour={tour} slide={s} variant="nested" />
          ))}
          {canToggle && (
            <TourExpander
              expanded={expanded}
              hiddenCount={hiddenCount}
              onToggle={() => setExpanded(v => !v)}
            />
          )}
        </div>
      )}
    </div>
  )
}
