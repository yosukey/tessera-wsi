/**
 * AdminSyncSessionsSection — staff-facing list of every active live-sync
 * session, with a force-end button per row.
 *
 * This is the operator's way out of the orphan trap: a presenter who lost the
 * tab they started a session from (closed it, switched devices, left a kiosk
 * on the slide) has no client-side path to end it, and a still-attached zombie
 * presenter keeps the server's grace GC from reaping the room. The backend
 * already lets any staff user end any session by id; this surfaces the list +
 * an End button so they can actually do it. Lives on the Server admin page.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  AdminSyncSessionInfo,
  adminListAllSyncSessions,
  endSyncSession,
  isAbortError,
  isUnauthorizedError,
} from '../../lib/api'
import { formatElapsed } from '../../lib/elapsed'
import { useI18n } from '../../lib/i18n'

const TABLE_STYLE: React.CSSProperties = { width: '100%', borderCollapse: 'collapse', fontSize: 12 }
const TH_STYLE: React.CSSProperties = {
  textAlign: 'left',
  padding: '4px 6px',
  fontWeight: 600,
  color: 'var(--color-text-muted)',
  whiteSpace: 'nowrap',
}
const TD_STYLE: React.CSSProperties = { padding: '4px 6px', verticalAlign: 'top' }

// Auto-refresh cadence. Sessions come and go (and the grace countdown ticks),
// so a periodic refresh keeps the panel honest without the operator clicking.
const REFRESH_INTERVAL_MS = 8_000

function formatAge(
  createdAtEpochSec: number,
  nowMs: number,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  // formatElapsed clamps at 0: server/client clock skew can make a
  // just-created session look marginally "negative" in age.
  return formatElapsed(Math.round(nowMs / 1000 - createdAtEpochSec), t)
}

function slideLabel(s: AdminSyncSessionInfo): string {
  return s.slide_title || s.slide_catalog_id || `#${s.slide_id}`
}

export default function AdminSyncSessionsSection() {
  const { t } = useI18n()
  const [sessions, setSessions] = useState<AdminSyncSessionInfo[] | null>(null)
  const [loadError, setLoadError] = useState<'auth' | 'failed' | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  // Per-session id currently being ended, so its row's button shows a pending
  // label and can't be double-clicked into a second DELETE.
  const [endingId, setEndingId] = useState<string | null>(null)
  const [endError, setEndError] = useState<string | null>(null)
  // Drives the age column's "live" feel without refetching every second.
  const [nowMs, setNowMs] = useState(() => Date.now())

  // Tracks mount state so async resolutions after unmount don't setState.
  const mountedRef = useRef(true)
  // Monotonic request id for latest-wins sequencing: the 8s poll has no
  // AbortController, so a slow tick N response can land after tick N+1 and
  // overwrite fresher data (a just-force-ended session reappearing as live,
  // follower counts jumping backward). Only the most-recently-issued load is
  // allowed to apply its result.
  const loadSeqRef = useRef(0)

  const load = useCallback(() => {
    const seq = ++loadSeqRef.current
    setRefreshing(true)
    setLoadError(null)
    adminListAllSyncSessions()
      .then((list) => {
        if (!mountedRef.current || seq !== loadSeqRef.current) return
        // Defensive: backend already sorts newest-first, but a client-side sort
        // keeps the order stable even if that ever changes.
        setSessions([...list].sort((a, b) => b.created_at - a.created_at))
      })
      .catch((err) => {
        if (isAbortError(err) || !mountedRef.current || seq !== loadSeqRef.current) return
        // Drop a list that can no longer be vouched for once the login is gone.
        if (isUnauthorizedError(err)) setSessions(null)
        setLoadError(isUnauthorizedError(err) ? 'auth' : 'failed')
      })
      .finally(() => {
        // Only the latest request clears the spinner; a superseded tick must
        // not flip it off while the newer request is still in flight.
        if (mountedRef.current && seq === loadSeqRef.current) setRefreshing(false)
      })
  }, [])

  useEffect(() => {
    mountedRef.current = true
    load()
    const refresh = setInterval(load, REFRESH_INTERVAL_MS)
    const tick = setInterval(() => setNowMs(Date.now()), 1000)
    return () => {
      mountedRef.current = false
      clearInterval(refresh)
      clearInterval(tick)
    }
  }, [load])

  const onEnd = useCallback(async (s: AdminSyncSessionInfo) => {
    if (endingId) return
    const ok = window.confirm(t('adminSync.confirmEnd', { slide: slideLabel(s) }))
    if (!ok) return
    setEndingId(s.session_id)
    setEndError(null)
    try {
      await endSyncSession(s.session_id)
      // Drop the row immediately for snappy feedback; the next refresh
      // reconciles against the server truth either way.
      if (mountedRef.current) {
        setSessions((prev) => (prev ? prev.filter((x) => x.session_id !== s.session_id) : prev))
        // Re-issue the list request. A poll already in flight was sent BEFORE
        // this DELETE, so its snapshot still holds the row just removed —
        // and with no newer request outstanding it is still the latest seq, so
        // the latest-wins guard would let it through and resurrect the ended
        // session as "Live" for up to a full tick. Loading again bumps the
        // sequence (retiring that response) and reconciles against the truth.
        load()
      }
    } catch {
      if (mountedRef.current) setEndError(t('adminSync.endFailed'))
    } finally {
      if (mountedRef.current) setEndingId(null)
    }
  }, [endingId, t, load])

  return (
    <section style={{ marginTop: 'var(--space-5)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
        <h3 style={{ margin: 0, fontSize: 'var(--font-size-lg)', fontWeight: 700 }}>
          {t('adminSync.title')}
          {sessions != null && (
            <span style={{ marginLeft: 8, fontSize: 12, fontWeight: 400, opacity: 0.6 }}>
              ({sessions.length})
            </span>
          )}
        </h3>
        <div style={{ flex: 1 }} />
        <button onClick={load} disabled={refreshing} style={{ fontSize: 12 }}>
          {refreshing ? t('adminSync.refreshing') : t('adminSync.refresh')}
        </button>
      </div>

      <div style={{ fontSize: 11, opacity: 0.6, margin: '6px 0 10px' }}>
        {t('adminSync.note')}
      </div>

      {loadError && (
        <div style={{ fontSize: 13, color: 'var(--color-danger-fg)', marginBottom: 8 }}>
          {loadError === 'auth' ? t('common.sessionExpired') : t('adminSync.loadFailed')}
        </div>
      )}
      {endError && (
        <div role="alert" style={{ fontSize: 13, color: 'var(--color-danger-fg)', marginBottom: 8 }}>
          {endError}
        </div>
      )}

      {sessions != null && sessions.length === 0 && !loadError && (
        <div style={{ fontSize: 13, opacity: 0.7 }}>{t('adminSync.empty')}</div>
      )}

      {sessions != null && sessions.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table style={TABLE_STYLE}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--color-border)' }}>
                <th style={TH_STYLE}>{t('adminSync.colSlide')}</th>
                <th style={TH_STYLE}>{t('adminSync.colOwner')}</th>
                <th style={TH_STYLE}>{t('adminSync.colStatus')}</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }}>{t('adminSync.colFollowers')}</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }}>{t('adminSync.colAge')}</th>
                <th style={{ ...TH_STYLE, textAlign: 'right' }} aria-label={t('adminSync.colActions')} />
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => {
                const orphaned = !s.presenter_connected
                return (
                  <tr key={s.session_id} style={{ borderBottom: '1px solid var(--color-border)' }}>
                    <td style={TD_STYLE}>
                      <div style={{ fontWeight: 600 }}>{slideLabel(s)}</div>
                      <div style={{ fontSize: 10, opacity: 0.55, fontFamily: 'monospace' }}>
                        {s.session_id}
                      </div>
                    </td>
                    <td style={TD_STYLE}>
                      {s.owner_username
                        ? <>
                            {s.owner_username}
                            {s.owner_role && (
                              <span style={{ marginLeft: 4, fontSize: 10, opacity: 0.55 }}>
                                ({s.owner_role})
                              </span>
                            )}
                          </>
                        : <span style={{ opacity: 0.55 }}>{t('adminSync.ownerUnknown')}</span>}
                    </td>
                    <td style={TD_STYLE}>
                      {orphaned ? (
                        <span style={{ color: 'var(--color-danger-fg, #c0392b)' }}>
                          {s.grace_remaining != null
                            ? t('adminSync.statusOrphan', { seconds: Math.round(s.grace_remaining) })
                            : t('adminSync.statusOrphanNoGrace')}
                        </span>
                      ) : (
                        <span style={{ color: 'var(--color-success-fg, #2e7d32)' }}>
                          ● {t('adminSync.statusLive')}
                        </span>
                      )}
                    </td>
                    <td style={{ ...TD_STYLE, textAlign: 'right', fontFamily: 'monospace' }}>
                      {s.follower_count}
                    </td>
                    <td style={{ ...TD_STYLE, textAlign: 'right', fontFamily: 'monospace' }}>
                      {formatAge(s.created_at, nowMs, t)}
                    </td>
                    <td style={{ ...TD_STYLE, textAlign: 'right' }}>
                      <button
                        onClick={() => onEnd(s)}
                        // One end at a time: every row's button waits for it,
                        // rather than ignoring the click.
                        disabled={endingId !== null}
                        style={{
                          fontSize: 12,
                          color: 'var(--color-danger-fg, #c0392b)',
                          borderColor: 'var(--color-danger-fg, #c0392b)',
                          cursor: endingId === s.session_id ? 'default' : 'pointer',
                        }}
                      >
                        {endingId === s.session_id ? t('adminSync.ending') : t('adminSync.end')}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
