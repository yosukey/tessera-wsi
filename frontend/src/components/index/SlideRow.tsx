import React from 'react'
import { Link } from 'react-router'
import type { Slide } from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { cssFilterString, imageAdjustFromSlide } from '../../lib/imageAdjust'
import { Card } from '../ui'

type Props = {
  slide: Slide
  tourCount?: number
  thumbWidth?: number
  thumbHeight?: number
  cardAttrs: string[]
}

export default function SlideRow({ slide: s, tourCount = 0, thumbWidth = 240, thumbHeight = 160, cardAttrs }: Props) {
  const { t } = useI18n()
  const attrs = s.attributes || {}
  const meta = cardAttrs
    .map(k => (attrs[k] || []).join(', '))
    .filter(Boolean)

  return (
    <Link
      to={`/slides/${s.public_id}`}
      target="_blank"
      rel="noopener noreferrer"
      style={{ textDecoration: 'none', color: 'inherit' }}
    >
      <Card interactive row padding="none">
        <div style={{ width: thumbWidth, height: thumbHeight, flexShrink: 0, background: 'var(--color-surface-muted)' }}>
          {s.thumb_url && (
            <img
              src={s.thumb_url}
              alt={s.title}
              loading="lazy"
              decoding="async"
              style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block', filter: cssFilterString(imageAdjustFromSlide(s)) }}
            />
          )}
        </div>
        <div style={{ padding: '10px 14px', flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
            {s.catalog_id && (
              <span style={{
                fontSize: 'var(--font-size-sm)',
                color: 'var(--color-text)',
                letterSpacing: '0.03em',
                flexShrink: 0,
              }}>
                {s.catalog_id}
              </span>
            )}
            <span style={{
              fontWeight: 600,
              fontSize: 'var(--font-size-md)',
              color: 'var(--color-text)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}>
              {s.title || t('index.slideFallback', { id: s.id })}
            </span>
            {tourCount > 0 && (
              <span style={{
                fontSize: 'var(--font-size-xs)',
                color: 'var(--color-brand-700)',
                background: 'var(--color-brand-50)',
                padding: '1px 8px',
                borderRadius: 'var(--radius-pill)',
                fontWeight: 600,
                flexShrink: 0,
              }}>
                {t('index.toursInline', { count: tourCount })}
              </span>
            )}
          </div>
          {meta.length > 0 && (
            <div style={{
              fontSize: 'var(--font-size-sm)',
              color: 'var(--color-text-subtle)',
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
