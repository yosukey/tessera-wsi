import { useEffect, useRef } from 'react'
import { APP_INFO } from '../lib/appInfo'
import { useDialogFocus } from '../hooks/useDialogFocus'

export default function AboutDialog({ onClose }: { onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement | null>(null)
  useDialogFocus(dialogRef)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      style={overlayStyle}
      role="dialog"
      aria-modal="true"
      aria-label="About Tessera"
      onClick={onClose}
    >
      <div ref={dialogRef} tabIndex={-1} style={dialogStyle} onClick={e => e.stopPropagation()}>
        <div style={headerStyle}>
          <span style={titleStyle}>{APP_INFO.name}</span>
          <button type="button" onClick={onClose} style={closeBtnStyle} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M1 1l12 12M13 1L1 13" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        <img
          src="/brand.png"
          alt={APP_INFO.name}
          style={brandImageStyle}
        />

        <p style={descStyle}>{APP_INFO.description}</p>

        <dl style={dlStyle}>
          <dt style={dtStyle}>Version</dt>
          <dd style={ddStyle}>v{APP_INFO.version}</dd>

          <dt style={dtStyle}>Repository</dt>
          <dd style={ddStyle}>
            <a href={APP_INFO.repoUrl} target="_blank" rel="noopener noreferrer" style={linkStyle}>
              {APP_INFO.repoUrl}
            </a>
          </dd>

          <dt style={dtStyle}>License</dt>
          <dd style={ddStyle}>
            <a href={APP_INFO.licenseUrl} target="_blank" rel="noopener noreferrer" style={linkStyle}>
              {APP_INFO.license}
            </a>
          </dd>

          <dt style={dtStyle}>Copyright</dt>
          <dd style={ddStyle}>{APP_INFO.copyright}</dd>

          {APP_INFO.paperUrl && (
            <>
              <dt style={dtStyle}>Paper</dt>
              <dd style={ddStyle}>
                <a href={APP_INFO.paperUrl} target="_blank" rel="noopener noreferrer" style={linkStyle}>
                  {APP_INFO.paperUrl}
                </a>
              </dd>
            </>
          )}
        </dl>

        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 20 }}>
          <button type="button" onClick={onClose} style={closeActionBtnStyle}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(0, 0, 0, 0.45)',
  zIndex: 'var(--z-modal, 1000)' as unknown as number,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 20,
}

const dialogStyle: React.CSSProperties = {
  background: '#fff',
  borderRadius: 'var(--radius-md, 10px)',
  padding: '20px 24px',
  width: '100%',
  maxWidth: 420,
  boxShadow: 'var(--shadow-lg, 0 10px 30px rgba(0,0,0,0.25))',
}

const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  marginBottom: 6,
}

const titleStyle: React.CSSProperties = {
  fontSize: 16,
  fontWeight: 700,
  color: 'var(--color-text, #111)',
  letterSpacing: '-0.01em',
}

const closeBtnStyle: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  cursor: 'pointer',
  color: 'var(--color-text-muted, #6b7280)',
  padding: 4,
  lineHeight: 1,
  borderRadius: 4,
}

const brandImageStyle: React.CSSProperties = {
  display: 'block',
  width: 400,
  maxWidth: '100%',
  objectFit: 'contain',
  margin: '0 auto 14px',
}

const descStyle: React.CSSProperties = {
  margin: '0 0 16px',
  fontSize: 13,
  color: 'var(--color-text-subtle, #9ca3af)',
  lineHeight: 1.5,
}

const dlStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: '90px 1fr',
  gap: '8px 12px',
  margin: 0,
  padding: 0,
}

const dtStyle: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 600,
  color: 'var(--color-text-muted, #6b7280)',
  textTransform: 'uppercase',
  letterSpacing: '0.06em',
  alignSelf: 'center',
}

const ddStyle: React.CSSProperties = {
  fontSize: 13,
  color: 'var(--color-text, #111)',
  margin: 0,
  wordBreak: 'break-all',
  alignSelf: 'center',
}

const linkStyle: React.CSSProperties = {
  color: 'var(--color-brand-700, #047857)',
  textDecoration: 'none',
}

const closeActionBtnStyle: React.CSSProperties = {
  background: 'transparent',
  border: '1px solid var(--color-border, #e5e7eb)',
  borderRadius: 6,
  padding: '6px 16px',
  fontSize: 13,
  cursor: 'pointer',
  color: 'var(--color-text-muted, #6b7280)',
}
