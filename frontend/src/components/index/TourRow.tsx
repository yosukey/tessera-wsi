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
  variant: 'nested' | 'flat'
  thumbWidth?: number
  thumbHeight?: number
}

export default function TourRow({ tour, slide, variant, thumbWidth = 240, thumbHeight = 160, cardAttrs = [] }: Props) {
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
      <Card interactive row padding="none">
        {variant === 'flat' && (
          <div style={{ width: thumbWidth, height: thumbHeight, flexShrink: 0, background: 'var(--color-surface-muted)' }}>
            {slide?.thumb_url && (
              <img
                src={slide.thumb_url}
                alt={slideLabel}
                loading="lazy"
                decoding="async"
                style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block', filter: cssFilterString(imageAdjustFromSlide(slide)) }}
              />
            )}
          </div>
        )}
        <div style={{ padding: '8px 14px', flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', gap: 10 }}>
          <TourBadge />
          <span style={{
            fontWeight: 600,
            fontSize: 'var(--font-size-sm)',
            color: 'var(--color-text)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            flexShrink: 1,
            minWidth: 0,
          }}>
            {tour.title || t('index.tourFallback', { id: tour.id })}
          </span>
          {variant === 'flat' && (
            <span style={{
              fontSize: 'var(--font-size-xs)',
              color: 'var(--color-text-subtle)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              flexShrink: 1,
              minWidth: 0,
            }}>
              {slide?.catalog_id ? `${slide.catalog_id} · ` : ''}{slideLabel}
            </span>
          )}
          {meta.length > 0 && (
            <span style={{
              fontSize: 'var(--font-size-xs)',
              color: 'var(--color-text-subtle)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              flexShrink: 1,
              minWidth: 0,
            }}>
              {meta.join(' · ')}
            </span>
          )}
        </div>
      </Card>
    </Link>
  )
}
