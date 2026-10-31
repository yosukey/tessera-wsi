/**
 * LiveSessionBanner — controlled-only banner for active live sync
 * sessions on the IndexPage. The parent owns the discovery WebSocket
 * (so the connection stays scoped to the index list page) and just
 * passes the latest active-sessions list down here.
 */
import { Link } from 'react-router'
import type { DiscoverySessionEntry } from '../lib/discovery'
import { useI18n } from '../lib/i18n'

export type LiveSessionForBanner = {
  sid: string
  slide_id: number
  /** The slide's public_id token — what the join link navigates to. */
  slide_public_id: string
  /** From listSlides, else the discovery channel's labels; null if neither has it. */
  slide_title: string | null
  slide_catalog_id: string | null
}

/**
 * The banner's entries for the discovery channel's sessions: each slide named
 * from the index page's loaded listing (``slideLabels``, by numeric id) when
 * it holds the slide, else from the labels the discovery entry carries — a
 * session on a slide past the loaded page is still named, not a bare "#4821".
 */
export function toBannerSessions(
  sessions: DiscoverySessionEntry[],
  slideLabels: ReadonlyMap<number, { title?: string | null; catalog_id?: string | null }>,
): LiveSessionForBanner[] {
  return sessions.map((s) => {
    const slide = slideLabels.get(s.slide_id)
    return {
      sid: s.sid,
      slide_id: s.slide_id,
      slide_public_id: s.slide_public_id,
      slide_title: slide?.title || s.slide_title || null,
      slide_catalog_id: slide?.catalog_id || s.slide_catalog_id || null,
    }
  })
}

export type LiveSessionBannerProps = {
  sessions: LiveSessionForBanner[]
}

export default function LiveSessionBanner({ sessions }: LiveSessionBannerProps) {
  const { t } = useI18n()
  if (sessions.length === 0) return null

  if (sessions.length === 1) {
    const s = sessions[0]
    const label = s.slide_title || s.slide_catalog_id || `#${s.slide_id}`
    return (
      <div style={bannerStyle()}>
        <span style={dotStyle} />
        <span style={titleStyle}>
          {t('sync.banner.titleSingle', { slide: label })}
        </span>
        <Link to={`/slides/${encodeURIComponent(s.slide_public_id)}?sync=${encodeURIComponent(s.sid)}`} style={joinBtnStyle}>
          {t('sync.banner.join')} →
        </Link>
      </div>
    )
  }

  // Multiple concurrent live sessions — show a stacked compact list.
  return (
    <div style={bannerStyle()}>
      <span style={dotStyle} />
      <span style={titleStyle}>
        {t('sync.banner.titleMany', { count: sessions.length })}
      </span>
      <ul style={listStyle}>
        {sessions.map((s) => {
          const label = s.slide_title || s.slide_catalog_id || `#${s.slide_id}`
          return (
            <li key={s.sid} style={listRowStyle}>
              <span style={{ flex: 1, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {label}
              </span>
              <Link to={`/slides/${encodeURIComponent(s.slide_public_id)}?sync=${encodeURIComponent(s.sid)}`} style={joinBtnStyle}>
                {t('sync.banner.join')} →
              </Link>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

// ---------- styles ------------------------------------------------------

function bannerStyle(): React.CSSProperties {
  return {
    background: '#fef9c3',
    borderBottom: '1px solid #facc15',
    color: '#713f12',
    padding: '8px 16px',
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    fontSize: 13,
    flexWrap: 'wrap',
  }
}

const dotStyle: React.CSSProperties = {
  width: 10,
  height: 10,
  borderRadius: '50%',
  background: '#dc2626',
  boxShadow: '0 0 6px rgba(220, 38, 38, 0.7)',
  flexShrink: 0,
}

const titleStyle: React.CSSProperties = {
  fontWeight: 600,
  flexShrink: 0,
}

const joinBtnStyle: React.CSSProperties = {
  background: '#dc2626',
  color: '#fff',
  border: '1px solid #b91c1c',
  borderRadius: 6,
  padding: '3px 12px',
  fontSize: 12,
  fontWeight: 600,
  textDecoration: 'none',
  letterSpacing: '0.02em',
}

const listStyle: React.CSSProperties = {
  listStyle: 'none',
  margin: 0,
  padding: 0,
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
  width: '100%',
  // With many concurrent presenters this list could otherwise grow tall enough
  // to push the header off-screen (the banner sits in the sticky top stack).
  // Cap it and let the rows scroll within the banner instead.
  maxHeight: '40vh',
  overflowY: 'auto',
  overscrollBehavior: 'contain',
}

const listRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
}
