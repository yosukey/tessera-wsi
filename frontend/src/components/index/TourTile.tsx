import React from 'react'
import { Link } from 'react-router'
import type { TourIndexSlide, TourSummary } from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { cssFilterString, imageAdjustFromSlide } from '../../lib/imageAdjust'
import { attributeParts, mergeAttributes } from '../../lib/attributes'
import { Card } from '../ui'
import TourBadge from './TourBadge'

type Props = {
  tour: TourSummary
  // Only the card fields: a full Slide (list view) or the parent data the
  // Tours view's /api/index/tours items carry.
  slide: TourIndexSlide | undefined
  // viewer_settings.index_card_attrs: the categories the card's metadata line
  // shows, from the slide's metadata plus the tour's own. Omitted, no line.
  cardAttrs?: string[]
  thumbHeight?: number
}

export default function TourTile({ tour, slide, thumbHeight = 148, cardAttrs = [] }: Props) {
  const { t } = useI18n()
  const meta = attributeParts(mergeAttributes(slide?.attributes, tour.attributes), cardAttrs)
  const slideLabel = slide
    ? (slide.title || t('index.slideFallback', { id: tour.slide_id }))
    : t('index.slideFallback', { id: tour.slide_id })

  return (
    <Link
      to={`/slides/${tour.slide_public_id}/tours/${tour.public_id}`}
      target="_blank"
      rel="noopener noreferrer"
      style={{ textDecoration: 'none', color: 'inherit' }}
    >
      <Card interactive padding="none">
        <Card.Media>
          <div style={{ position: 'relative', height: thumbHeight, background: 'var(--color-surface-muted)' }}>
            {slide?.thumb_url ? (
              <img
                src={slide.thumb_url}
                alt={slideLabel}
                loading="lazy"
                decoding="async"
                style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block', filter: cssFilterString(imageAdjustFromSlide(slide)) }}
              />
            ) : (
              <div style={{ height: '100%' }} />
            )}
            <div style={{ position: 'absolute', top: 8, left: 8 }}>
              <TourBadge />
            </div>
          </div>
        </Card.Media>
        <div style={{ padding: '12px 14px' }}>
          <div style={{
            fontWeight: 600,
            fontSize: 'var(--font-size-md)',
            lineHeight: 1.4,
            color: 'var(--color-text)',
            marginBottom: 4,
          }}>
            {tour.title || t('index.tourFallback', { id: tour.id })}
          </div>
          <div style={{
            fontSize: 'var(--font-size-sm)',
            color: 'var(--color-text-subtle)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}>
            {slide?.catalog_id ? `${slide.catalog_id} · ` : ''}{slideLabel}
          </div>
          {meta.length > 0 && (
            <div style={{
              fontSize: 'var(--font-size-sm)',
              color: 'var(--color-text-subtle)',
              lineHeight: 1.5,
              marginTop: 4,
            }}>
              {meta.join(' · ')}
            </div>
          )}
        </div>
      </Card>
    </Link>
  )
}
