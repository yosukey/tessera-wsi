/**
 * SlideSwitcher — floating button + popover for switching the active
 * slide on ViewerPage. Rendered only while a live-sync session is active
 * (ViewerPage gates it on `liveSession`), so in practice this is the
 * presenter's slide-changer. The current query string — ``?from=admin`` and
 * any ``debug`` flag — is preserved on navigate so the presenter chrome
 * survives the switch. (The session itself lives in the page's hook, not in
 * the URL: a ``?sync=`` would make the page a follower.)
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router'
import { isAbortError, isRateLimitError, listSlides, MAX_INDEX_ITEMS_MAX, type Slide } from '../lib/api'
import { useI18n } from '../lib/i18n'

export default function SlideSwitcher() {
  const { t } = useI18n()
  const { id } = useParams()
  // The URL segment is the current slide's public_id token.
  const currentPublicId = id ?? ''
  const [searchParams] = useSearchParams()
  const nav = useNavigate()

  const [open, setOpen] = useState(false)
  const [slides, setSlides] = useState<Slide[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const popoverRef = useRef<HTMLDivElement | null>(null)

  // Lazy-load the slide list the first time the popover opens.
  useEffect(() => {
    if (!open || slides !== null) return
    setLoadError(null)
    const params = new URLSearchParams()
    // Request up to the instance-wide index cap (MAX_INDEX_ITEMS_MAX, also the
    // backend's per_page max) so a presenter can switch to any published slide,
    // not just the first page of the default sort.
    params.set('per_page', String(MAX_INDEX_ITEMS_MAX))
    // Abortable: this asks for the full index cap, so a popover opened and
    // dismissed (or a session that ends and unmounts the switcher) should not
    // leave a large response in flight.
    const ctrl = new AbortController()
    listSlides(params, { signal: ctrl.signal })
      .then((r) => setSlides(r.items))
      .catch((e) => {
        if (isAbortError(e)) return
        setLoadError(isRateLimitError(e) ? t('common.rateLimited') : t('viewer.switchSlide.loadFailed'))
      })
    return () => ctrl.abort()
  }, [open, slides])

  // Click outside to close.
  useEffect(() => {
    if (!open) return
    function onDocClick(ev: MouseEvent) {
      if (!popoverRef.current) return
      if (!popoverRef.current.contains(ev.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [open])

  const filtered = useMemo(() => {
    if (!slides) return []
    const q = filter.trim().toLowerCase()
    if (!q) return slides
    return slides.filter((s) => {
      const cid = (s.catalog_id || '').toLowerCase()
      const title = (s.title || '').toLowerCase()
      return cid.includes(q) || title.includes(q)
    })
  }, [slides, filter])

  function navigateToSlide(slidePublicId: string) {
    if (slidePublicId === currentPublicId) {
      setOpen(false)
      return
    }
    const qs = new URLSearchParams(searchParams)
    // Carry the current query string forward unchanged: the mode flags
    // (debug / from=admin) must survive the switch so a presenter who
    // changes slides mid-session keeps the presenter chrome.
    const suffix = qs.toString() ? `?${qs.toString()}` : ''
    setOpen(false)
    nav(`/slides/${encodeURIComponent(slidePublicId)}${suffix}`)
  }

  return (
    <div
      ref={popoverRef}
      style={{ position: 'absolute', bottom: 'calc(90px + env(safe-area-inset-bottom, 0px))', left: 'calc(12px + env(safe-area-inset-left, 0px))', zIndex: 11 }}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={t('viewer.switchSlide.toggle')}
        style={buttonStyle(open)}
      >
        {t('viewer.switchSlide.button')}
      </button>
      {open && (
        <div style={panelStyle}>
          <input
            autoFocus
            placeholder={t('viewer.switchSlide.search')}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            style={searchStyle}
          />
          <div className="overscroll-panel" style={listStyle}>
            {loadError && <div style={errorStyle}>{loadError}</div>}
            {!loadError && slides === null && (
              <div style={emptyStyle}>{t('viewer.switchSlide.loading')}</div>
            )}
            {!loadError && slides !== null && filtered.length === 0 && (
              <div style={emptyStyle}>{t('viewer.switchSlide.noMatch')}</div>
            )}
            {filtered.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => navigateToSlide(s.public_id)}
                style={rowStyle(s.public_id === currentPublicId)}
              >
                {s.thumb_url ? (
                  <img
                    src={s.thumb_url}
                    alt=""
                    style={thumbStyle}
                    loading="lazy"
                  />
                ) : (
                  <div style={{ ...thumbStyle, background: '#e5e7eb' }} />
                )}
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={catalogStyle}>{s.catalog_id || `#${s.id}`}</div>
                  <div style={titleStyle}>{s.title || '—'}</div>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ---------- styles (kept inline to stay self-contained, no extra CSS) ----

function buttonStyle(active: boolean): React.CSSProperties {
  return {
    background: active ? '#dbeafe' : 'rgba(255, 255, 255, 0.92)',
    color: active ? '#1d4ed8' : 'var(--color-text, #111)',
    border: `1px solid ${active ? '#1d4ed8' : 'var(--color-border, #ccc)'}`,
    borderRadius: 'var(--radius-md, 6px)',
    padding: '4px 10px',
    fontSize: 12,
    fontWeight: 600,
    cursor: 'pointer',
    boxShadow: 'var(--shadow-sm)',
    letterSpacing: '0.02em',
  }
}

const panelStyle: React.CSSProperties = {
  position: 'absolute',
  bottom: 36,
  left: 0,
  width: 320,
  maxHeight: 420,
  background: 'rgba(255, 255, 255, 0.98)',
  border: '1px solid var(--color-border, #ccc)',
  borderRadius: 'var(--radius-md, 8px)',
  boxShadow: 'var(--shadow-md, 0 4px 12px rgba(0,0,0,0.15))',
  display: 'flex',
  flexDirection: 'column',
  overflow: 'hidden',
}

const searchStyle: React.CSSProperties = {
  border: 'none',
  borderBottom: '1px solid var(--color-border, #eee)',
  padding: '8px 10px',
  fontSize: 13,
  outline: 'none',
}

const listStyle: React.CSSProperties = {
  overflowY: 'auto',
  display: 'flex',
  flexDirection: 'column',
}

const emptyStyle: React.CSSProperties = {
  padding: '12px',
  fontSize: 12,
  color: 'var(--color-text-muted, #666)',
}

const errorStyle: React.CSSProperties = {
  padding: '12px',
  fontSize: 12,
  color: '#b91c1c',
}

function rowStyle(active: boolean): React.CSSProperties {
  return {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    padding: '6px 10px',
    background: active ? '#dbeafe' : 'transparent',
    border: 'none',
    borderBottom: '1px solid var(--color-border, #f3f4f6)',
    cursor: 'pointer',
    textAlign: 'left',
    width: '100%',
  }
}

const thumbStyle: React.CSSProperties = {
  width: 44,
  height: 44,
  objectFit: 'cover',
  borderRadius: 4,
  flexShrink: 0,
  background: '#fafafa',
}

const catalogStyle: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 600,
  color: 'var(--color-text, #111)',
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
}

const titleStyle: React.CSSProperties = {
  fontSize: 11,
  color: 'var(--color-text-muted, #555)',
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
}
