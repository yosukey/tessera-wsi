import React, { useState } from 'react'
import { Link } from 'react-router'
import type { Slide, TourSummary } from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { cssFilterString, imageAdjustFromSlide } from '../../lib/imageAdjust'
import { Card } from '../ui'
import TourExpander from './TourExpander'

type Props = {
  slide: Slide
  tours: TourSummary[]
  thumbHeight?: number
  cardAttrs: string[]
  // Admin-configured cap on how many tours are listed before the rest
  // collapse behind the expander (viewer_settings.index_tours_per_slide).
  // 0 is a valid value — list none until the user asks.
  toursPerSlide: number
}

export default function SlideCard({ slide: s, tours: slideTours, thumbHeight = 148, cardAttrs, toursPerSlide }: Props) {
  const { t } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const attrs = s.attributes || {}
  const meta = cardAttrs
    .map(k => (attrs[k] || []).join(', '))
    .filter(Boolean)
  const visibleTours = expanded ? slideTours : slideTours.slice(0, toursPerSlide)
  const hiddenCount = slideTours.length - visibleTours.length
  // Driven by the cap, not by hiddenCount: once expanded, hiddenCount is 0,
  // and gating on it would make the expansion one-way (no way back to the
  // compact card short of remounting it).
  const canToggle = slideTours.length > toursPerSlide

  return (
    <Card interactive padding="none">
      <Link
        to={`/slides/${s.public_id}`}
        target="_blank"
        rel="noopener noreferrer"
        style={{ textDecoration: 'none', color: 'inherit', display: 'block' }}
      >
        <Card.Media>
          {s.thumb_url
            ? <img
                src={s.thumb_url}
                alt={s.title}
                loading="lazy"
                decoding="async"
                style={{ width: '100%', height: thumbHeight, objectFit: 'contain', display: 'block', filter: cssFilterString(imageAdjustFromSlide(s)) }}
              />
            : <div style={{ height: thumbHeight }} />
          }
        </Card.Media>
        <div style={{ padding: '12px 14px' }}>
          {s.catalog_id && (
            <div style={{
              fontSize: 'var(--font-size-sm)',
              color: 'var(--color-text)',
              marginBottom: 4,
              letterSpacing: '0.03em',
            }}>
              {s.catalog_id}
            </div>
          )}
          <div style={{
            fontWeight: 600,
            fontSize: 'var(--font-size-md)',
            lineHeight: 1.4,
            color: 'var(--color-text)',
            marginBottom: 6,
          }}>
            {s.title || t('index.slideFallback', { id: s.id })}
          </div>
          {meta.length > 0 && (
            <div style={{
              fontSize: 'var(--font-size-sm)',
              color: 'var(--color-text-subtle)',
              lineHeight: 1.5,
            }}>
              {meta.join(' · ')}
            </div>
          )}
        </div>
      </Link>
      {slideTours.length > 0 && (
        <div style={{
          borderTop: '1px solid var(--color-border)',
          padding: '8px 14px 10px',
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
        }}>
          <div style={{
            fontSize: 10,
            fontWeight: 700,
            textTransform: 'uppercase',
            letterSpacing: '0.08em',
            color: 'var(--color-text-faint)',
            marginBottom: 2,
          }}>
            {t('index.toursInline', { count: slideTours.length })}
          </div>
          {visibleTours.map(tour => (
            <Link
              key={tour.id}
              to={`/slides/${tour.slide_public_id}/tours/${tour.public_id}`}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                textDecoration: 'none',
                color: 'var(--color-brand-700)',
                fontSize: 'var(--font-size-sm)',
                lineHeight: 1.4,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                fontWeight: 500,
              }}
            >
              · {tour.title || t('index.tourFallback', { id: tour.id })}
            </Link>
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
    </Card>
  )
}
