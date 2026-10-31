import React, { useState } from 'react'
import { useI18n } from '../lib/i18n'
import { useMediaQuery } from '../hooks/useMediaQuery'
import { SlideCredit as SlideCreditFields, hasSlideCredit, licenseLabel, licenseUrl, safeSourceUrl } from '../lib/slideCredit'

// Below this width the bottom-right column can reach the bottom-left tools
// (image-adjust toggle, scale bar) horizontally.
const NARROW_VIEWER = '(max-width: 600px)'

// Bottom-right column shared by the viewer and the tour viewer: the slide's
// metadata panel (optional) stacked above its image credit (and, in the
// viewer, the About button). Absolutely positioned as one unit so its items
// never overlap whichever are shown. On a narrow screen the column moves up
// above the bottom-left tool row and keeps clear of the debug toggle's
// column, so a wide metadata card can't cover the scale bar.
// Click-through by default; children that need clicks opt back in.
export function ViewerInfoCorner({ children }: { children: React.ReactNode }) {
  const narrow = useMediaQuery(NARROW_VIEWER)
  return (
    <div style={{
      position: 'absolute',
      bottom: narrow
        ? 'calc(52px + env(safe-area-inset-bottom, 0px))'
        : 'calc(12px + env(safe-area-inset-bottom, 0px))',
      right: 'calc(12px + env(safe-area-inset-right, 0px))',
      zIndex: 10,
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'flex-end',
      gap: 6,
      // The metadata panel's own max width.
      maxWidth: narrow ? 'calc(100% - 132px)' : 'min(360px, calc(100% - 24px))',
      pointerEvents: 'none',
    }}>
      {children}
    </div>
  )
}

const linkStyle: React.CSSProperties = { color: 'inherit', textDecoration: 'underline' }

// The image credit of the slide on screen: rights holder, license (linked to
// its deed) and source link. Rendered whenever the slide has any of them —
// deliberately independent of show_metadata_panel, since it is the image's
// attribution rather than optional slide info. On a narrow screen it starts
// collapsed to a "©" button so it does not cover the slide.
export default function SlideCredit({ credit }: { credit: SlideCreditFields | null | undefined }) {
  const { t } = useI18n()
  const narrow = useMediaQuery(NARROW_VIEWER)
  const [expanded, setExpanded] = useState(false)
  if (!credit || !hasSlideCredit(credit, t)) return null

  const text = (credit.credit_text || '').trim()
  const license = licenseLabel(credit.license_id, t)
  const licUrl = licenseUrl(credit.license_id)
  const source = safeSourceUrl(credit.source_url)

  const pill: React.CSSProperties = {
    pointerEvents: 'auto',
    background: 'rgba(255, 255, 255, 0.82)',
    color: 'rgba(0, 0, 0, 0.85)',
    borderRadius: 5,
    fontSize: 11,
    lineHeight: 1.4,
  }

  if (narrow && !expanded) {
    return (
      <button
        type="button"
        onClick={() => setExpanded(true)}
        title={t('viewer.credit.toggle')}
        aria-label={t('viewer.credit.toggle')}
        aria-expanded={false}
        style={{ ...pill, border: 'none', cursor: 'pointer', padding: '4px 8px', fontWeight: 700 }}
      >
        ©
      </button>
    )
  }

  return (
    <aside
      aria-label={t('viewer.credit.label')}
      onClick={narrow ? () => setExpanded(false) : undefined}
      style={{
        ...pill,
        padding: '4px 8px',
        textAlign: 'right',
        overflowWrap: 'anywhere',
      }}
    >
      {text && (
        <div
          title={text}
          style={{
            display: '-webkit-box',
            WebkitLineClamp: narrow ? undefined : 3,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}
        >
          {text}
        </div>
      )}
      {(license || source) && (
        <div style={{ opacity: 0.85 }}>
          {license && (licUrl
            ? <a href={licUrl} target="_blank" rel="license noopener noreferrer" style={linkStyle} onClick={e => e.stopPropagation()}>{license}</a>
            : <span>{license}</span>)}
          {license && source && <span aria-hidden> · </span>}
          {source && (
            <a href={source} target="_blank" rel="noopener noreferrer" style={linkStyle} onClick={e => e.stopPropagation()}>
              {t('viewer.credit.source')} ↗
            </a>
          )}
        </div>
      )}
    </aside>
  )
}
