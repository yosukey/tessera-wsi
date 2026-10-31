import React from 'react'
import { useI18n } from '../lib/i18n'
import type { Slide, ViewerSettings } from '../lib/api'
import { attributeParts, mergeAttributes, type AttributeMap } from '../lib/attributes'

type Props = {
  slide: Slide | null
  settings: Pick<ViewerSettings, 'extra_attrs' | 'logo_url' | 'copyright_msg'>
  // In the tour viewer, the tour's own metadata, shown together with the
  // slide's (a tour carries both).
  tourAttributes?: AttributeMap | null
}

// The slide's metadata card in the bottom-right corner of the viewer and the
// tour viewer: catalog ID, title, the admin-selected extra attributes (the
// slide's, plus in the tour viewer the tour's own), the
// site logo and copyright line. Renders nothing at all when none of those has
// a value (e.g. while the slide is loading or after it failed to), rather
// than an empty frosted card.
export default function SlideInfoPanel({ slide, settings, tourAttributes }: Props) {
  const { t } = useI18n()
  const extraParts = attributeParts(
    mergeAttributes(slide?.attributes, tourAttributes),
    (settings.extra_attrs || []).filter(key => key !== 'catalog_id' && key !== 'title'),
  )
  const catalogId = slide?.catalog_id || ''
  const title = slide?.title || ''
  if (!catalogId && !title && extraParts.length === 0 && !settings.logo_url && !settings.copyright_msg) {
    return null
  }

  return (
    <div style={{
      background: 'rgba(255, 255, 255, 0.92)',
      backdropFilter: 'blur(6px)',
      WebkitBackdropFilter: 'blur(6px)',
      padding: '10px 14px',
      borderRadius: 'var(--radius-lg)',
      border: '1px solid var(--color-border)',
      boxShadow: 'var(--shadow-sm)',
      textAlign: 'right',
      maxWidth: 360,
      pointerEvents: 'none',
    }}>
      {catalogId && (
        <div style={{
          fontSize: 'var(--font-size-sm)',
          color: 'var(--color-text-muted)',
          letterSpacing: '0.03em',
        }}>
          {catalogId}
        </div>
      )}
      {title && (
        <div style={{
          fontWeight: 700,
          fontSize: 'var(--font-size-lg)',
          color: 'var(--color-text)',
          letterSpacing: '-0.01em',
          marginTop: catalogId ? 3 : 0,
        }}>
          {title}
        </div>
      )}
      {extraParts.length > 0 && (
        <div style={{
          fontSize: 'var(--font-size-xs)',
          color: 'var(--color-text-subtle)',
          marginTop: (catalogId || title) ? 8 : 0,
        }}>
          {extraParts.join(' / ')}
        </div>
      )}
      {settings.logo_url && (
        <div style={{ marginTop: (catalogId || title || extraParts.length > 0) ? 8 : 0 }}>
          <img src={settings.logo_url} style={{ maxHeight: 40, maxWidth: 200, objectFit: 'contain', marginLeft: 'auto' }} alt={t('common.logoAlt')} />
        </div>
      )}
      {settings.copyright_msg && (
        <div style={{
          fontSize: 'var(--font-size-xs)',
          color: 'var(--color-text-faint)',
          marginTop: (catalogId || title || extraParts.length > 0 || settings.logo_url) ? 8 : 0,
        }}>
          {settings.copyright_msg}
        </div>
      )}
    </div>
  )
}
