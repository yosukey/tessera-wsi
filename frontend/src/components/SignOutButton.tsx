import React, { useEffect, useState } from 'react'
import { appLogout, getAuthState } from '../lib/api'
import { useI18n } from '../lib/i18n'

/**
 * "Sign out" for a visitor signed in to the public pages through LDAP or
 * Google. Renders nothing in the other app-access modes (none / basic have no
 * app session to end) or for a visitor without an app session of their own —
 * including staff let in by their dashboard token alone, whom signing out
 * would only bounce off /login back to the page. Ending the session matters
 * on shared PCs, where the session cookie would otherwise stay valid for its
 * whole lifetime.
 */
export default function SignOutButton({ style }: { style?: React.CSSProperties }) {
  const { t } = useI18n()
  const [shown, setShown] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    getAuthState()
      .then((s) => {
        if (!cancelled) setShown((s.mode === 'ldap' || s.mode === 'google_oauth') && s.app_session)
      })
      .catch(() => { /* no state, no button */ })
    return () => { cancelled = true }
  }, [])

  if (!shown) return null

  const signOut = async () => {
    // Asked first, so a stray click doesn't end the session.
    if (!window.confirm(t('login.signOutConfirm'))) return
    setBusy(true)
    try {
      await appLogout()
    } catch {
      // Unreachable server, or one that answered without ending the session
      // (rate limit, app down): the session is still there, so stay put —
      // /login would only bounce a signed-in visitor back here — and say so,
      // rather than leave them believing a shared PC is signed out. A native
      // dialog like the confirm above: the button sits in tight page headers.
      setBusy(false)
      window.alert(t('login.signOutFailed'))
      return
    }
    // A full page load, so nothing from the signed-in session stays in memory.
    window.location.assign('/login')
  }

  return (
    <button
      type="button"
      className="btn-reset"
      onClick={signOut}
      disabled={busy}
      style={{
        fontSize: 'var(--font-size-xs)',
        color: 'var(--color-text-faint)',
        cursor: busy ? 'default' : 'pointer',
        flexShrink: 0,
        whiteSpace: 'nowrap',
        ...style,
      }}
    >
      {t('login.signOut')}
    </button>
  )
}
