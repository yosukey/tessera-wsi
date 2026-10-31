/**
 * FollowerJoinDialog — name-input modal shown before connecting a
 * follower's WebSocket. Lets the visitor confirm what live session they
 * are about to join (catalog id + title from the server-side peek) and
 * pick a display name that the presenter will see.
 */
import { useEffect, useId, useRef, useState } from 'react'
import { useDialogFocus } from '../hooks/useDialogFocus'
import { useI18n } from '../lib/i18n'
import { safeGetItem, safeRemoveItem, safeSetItem } from '../lib/safeStorage'
import { STORAGE_KEYS } from '../lib/storageKeys'
import { ClearButton } from './ui'

const NAME_STORAGE_KEY = STORAGE_KEYS.syncDisplayName
const NAME_MAX_LEN = 64

// The zero-width (non-)joiner, which emoji sequences and several scripts are
// built with: the only format characters a name keeps.
const NAME_JOINERS = new Set(['\u200c', '\u200d'])

// The server's rule (sync_room.sanitize_name), so a name this dialog accepts
// is not empty there — a required name would then 4400 a follower who had typed
// one made only of, say, zero-width spaces. Controls, surrogates and
// private-use code points go, format characters but the joiners too (the bidi
// overrides among them); line / paragraph separators become a space; a wide
// space such as the ideographic one (U+3000) in a name like "山田 太郎" stays.
// Unassigned code points stay, as on the server: the two run different
// Unicode versions, so what one side does not know yet must not be dropped.
// Cut to 64 code points (the server's [:64]; the textbox's maxLength counts
// UTF-16 units) before the "anything visible?" check, so leading joiners
// cannot push the visible part past the cut.
export function sanitizeName(raw: string): string {
  const kept = Array.from(raw)
    .map((ch) => (/[\u2028\u2029]/.test(ch) ? ' ' : ch))
    .filter((ch) => (/\p{Cf}/u.test(ch) ? NAME_JOINERS.has(ch) : !/[\p{Cc}\p{Cs}\p{Co}]/u.test(ch)))
    .join('')
    .trim()
  const cleaned = Array.from(kept).slice(0, NAME_MAX_LEN).join('').trim()
  // Nothing visible left: only spaces and joiners.
  if (!Array.from(cleaned).some((ch) => !/\s/u.test(ch) && !NAME_JOINERS.has(ch))) return ''
  return cleaned
}

export type FollowerJoinDialogProps = {
  /** Display info from /api/sync/sessions/{sid} for context. */
  slideRef: { catalog_id: string | null; title: string | null } | null
  /** When true, hide the "Join without a name" button. */
  requireName?: boolean
  onJoin: (name: string | null) => void
  onCancel: () => void
}

export default function FollowerJoinDialog({
  slideRef,
  requireName,
  onJoin,
  onCancel,
}: FollowerJoinDialogProps) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [remember, setRemember] = useState(true)
  const [showRequired, setShowRequired] = useState(false)
  const titleId = useId()
  // Focus starts in the name field (the first control).
  const panelRef = useRef<HTMLDivElement | null>(null)
  useDialogFocus(panelRef)

  useEffect(() => {
    const saved = safeGetItem(NAME_STORAGE_KEY)
    if (saved) setName(saved)
  }, [])

  function persistOrClear(finalName: string | null) {
    if (remember && finalName) safeSetItem(NAME_STORAGE_KEY, finalName)
    else if (!remember) safeRemoveItem(NAME_STORAGE_KEY)
  }

  function submit() {
    const cleaned = sanitizeName(name)
    if (!cleaned) {
      if (requireName) {
        setShowRequired(true)
        return
      }
      persistOrClear(null)
      onJoin(null)
      return
    }
    persistOrClear(cleaned)
    onJoin(cleaned)
  }

  function joinAnonymous() {
    persistOrClear(null)
    onJoin(null)
  }

  return (
    <div style={overlayStyle} role="dialog" aria-modal="true" aria-labelledby={titleId}>
      <div
        ref={panelRef}
        tabIndex={-1}
        style={dialogStyle}
        onKeyDown={(e) => { if (e.key === 'Escape') onCancel() }}
      >
        <h2 id={titleId} style={titleStyle}>{t('sync.joinTitle')}</h2>
        {slideRef && (slideRef.catalog_id || slideRef.title) && (
          <div style={slideRefStyle}>
            {slideRef.catalog_id && (
              <span style={{ fontWeight: 600, marginRight: 6 }}>
                {slideRef.catalog_id}
              </span>
            )}
            <span>{slideRef.title || ''}</span>
          </div>
        )}
        <label style={labelStyle}>
          <span style={{ fontSize: 13 }}>{t('sync.joinNameLabel')}</span>
          <div style={inputWrapStyle}>
            <input
              type="text"
              value={name}
              onChange={(e) => {
                setName(e.target.value)
                if (showRequired) setShowRequired(false)
              }}
              onKeyDown={(e) => {
                // Ignore the Enter that confirms an IME (kana->kanji)
                // conversion, otherwise a Japanese name is submitted
                // mid-composition with a half-converted value.
                if (e.key === 'Enter' && !e.nativeEvent.isComposing) submit()
              }}
              placeholder={t('sync.joinNamePlaceholder') as string}
              maxLength={NAME_MAX_LEN}
              style={{ ...inputStyle, paddingRight: name ? 34 : 10 }}
            />
            {name && (
              <span style={clearWrapStyle}>
                <ClearButton
                  onClick={() => {
                    setName('')
                    if (showRequired) setShowRequired(false)
                  }}
                  label={t('sync.joinNameClear') as string}
                />
              </span>
            )}
          </div>
        </label>
        {showRequired && (
          <div style={errorStyle}>{t('sync.joinNameRequired')}</div>
        )}
        <label style={checkboxRowStyle}>
          <input
            type="checkbox"
            checked={remember}
            onChange={(e) => setRemember(e.target.checked)}
          />
          <span style={{ fontSize: 12 }}>{t('sync.joinRemember')}</span>
        </label>
        <div style={actionRowStyle}>
          <button type="button" onClick={onCancel} style={cancelBtnStyle}>
            {t('sync.joinCancel')}
          </button>
          {!requireName && (
            <button
              type="button"
              onClick={joinAnonymous}
              style={anonBtnStyle}
            >
              {t('sync.joinAnonymous')}
            </button>
          )}
          <button type="button" onClick={submit} style={primaryBtnStyle}>
            {t('sync.joinSubmit')}
          </button>
        </div>
      </div>
    </div>
  )
}

// ---------- styles ------------------------------------------------------

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(0, 0, 0, 0.55)',
  zIndex: 100,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 20,
}

const dialogStyle: React.CSSProperties = {
  background: '#fff',
  borderRadius: 'var(--radius-md, 10px)',
  padding: 20,
  width: '100%',
  maxWidth: 380,
  boxShadow: 'var(--shadow-lg, 0 10px 30px rgba(0,0,0,0.25))',
}

const titleStyle: React.CSSProperties = {
  margin: '0 0 12px 0',
  fontSize: 16,
  fontWeight: 600,
}

const slideRefStyle: React.CSSProperties = {
  background: '#f3f4f6',
  borderRadius: 6,
  padding: '8px 10px',
  fontSize: 13,
  marginBottom: 12,
}

const labelStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 6,
  marginBottom: 4,
}

const inputWrapStyle: React.CSSProperties = {
  position: 'relative',
  display: 'flex',
  alignItems: 'center',
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  border: '1px solid var(--color-border, #ccc)',
  borderRadius: 6,
  padding: '8px 10px',
  fontSize: 14,
  outline: 'none',
}

const clearWrapStyle: React.CSSProperties = {
  position: 'absolute',
  right: 8,
  top: '50%',
  transform: 'translateY(-50%)',
  display: 'flex',
  alignItems: 'center',
}

const errorStyle: React.CSSProperties = {
  color: '#b91c1c',
  fontSize: 12,
  marginTop: 4,
}

const checkboxRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  marginTop: 12,
  marginBottom: 16,
}

const actionRowStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'flex-end',
  gap: 8,
  flexWrap: 'wrap',
}

const cancelBtnStyle: React.CSSProperties = {
  background: 'transparent',
  border: '1px solid var(--color-border, #ccc)',
  borderRadius: 6,
  padding: '6px 12px',
  fontSize: 13,
  cursor: 'pointer',
}

const anonBtnStyle: React.CSSProperties = {
  background: 'transparent',
  border: '1px solid var(--color-border, #ccc)',
  borderRadius: 6,
  padding: '6px 12px',
  fontSize: 13,
  cursor: 'pointer',
  color: '#374151',
}

const primaryBtnStyle: React.CSSProperties = {
  background: '#1d4ed8',
  color: '#fff',
  border: '1px solid #1d4ed8',
  borderRadius: 6,
  padding: '6px 14px',
  fontSize: 13,
  fontWeight: 600,
  cursor: 'pointer',
}
