/**
 * SyncPresenterLauncher — the idle "Start live sync" affordance.
 *
 * Pure presentational component. The session lifecycle lives in
 * useSyncPresenterSession; this view only renders the bottom-left
 * launcher button and the inline error pill that surfaces a failed
 * start (e.g. expired admin JWT -> 401) so the click never looks dead.
 *
 * When ``resumable`` is set, the admin owns a still-live session for this
 * slide that they previously left; the button switches to "resume" and
 * reattaches to that session instead of minting a new one. With
 * ``resumeTakesOver`` the server still sees a presenter attached to it —
 * another tab or device, or this browser's dropped socket it has not noticed
 * yet — and a note says Resume moves the session here.
 */
import { useI18n } from '../lib/i18n'

export type SyncPresenterLauncherProps = {
  onStart: () => void
  onResume: () => void
  resumable: boolean
  resumeTakesOver?: boolean
  error: string | null
}

export default function SyncPresenterLauncher({
  onStart,
  onResume,
  resumable,
  resumeTakesOver = false,
  error,
}: SyncPresenterLauncherProps) {
  const { t } = useI18n()
  const label = resumable ? (t('sync.resume') as string) : (t('sync.start') as string)
  return (
    <div style={launchWrapperStyle}>
      <button
        type="button"
        onClick={resumable ? onResume : onStart}
        title={error || label}
        style={launchButtonStyle}
      >
        {resumable ? '⟳' : '▶'} {label}
      </button>
      {resumable && resumeTakesOver && (
        <div role="note" style={launchNoteStyle}>
          {t('sync.resumeTakesOver')}
        </div>
      )}
      {error && (
        <div role="alert" style={launchErrorStyle}>
          {error}
        </div>
      )}
    </div>
  )
}

// ---------- styles ------------------------------------------------------

const launchWrapperStyle: React.CSSProperties = {
  position: 'absolute',
  bottom: 90,
  left: 130,
  zIndex: 11,
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'flex-start',
  gap: 6,
  maxWidth: 280,
}

const launchButtonStyle: React.CSSProperties = {
  background: 'rgba(255, 255, 255, 0.92)',
  color: 'var(--color-text, #111)',
  border: '1px solid var(--color-border, #ccc)',
  borderRadius: 'var(--radius-md, 6px)',
  padding: '4px 10px',
  fontSize: 12,
  fontWeight: 600,
  cursor: 'pointer',
  boxShadow: 'var(--shadow-sm)',
  letterSpacing: '0.02em',
}

const launchNoteStyle: React.CSSProperties = {
  background: 'rgba(255, 255, 255, 0.92)',
  border: '1px solid var(--color-border, #ccc)',
  color: 'var(--color-text, #111)',
  borderRadius: 6,
  padding: '4px 8px',
  fontSize: 11,
  lineHeight: 1.4,
  boxShadow: 'var(--shadow-sm)',
}

const launchErrorStyle: React.CSSProperties = {
  background: '#fef2f2',
  border: '1px solid #fecaca',
  color: '#b91c1c',
  borderRadius: 6,
  padding: '4px 8px',
  fontSize: 11,
  lineHeight: 1.4,
  boxShadow: 'var(--shadow-sm)',
}
