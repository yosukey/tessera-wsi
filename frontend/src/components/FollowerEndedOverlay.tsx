/**
 * FollowerEndedOverlay — the follower's terminal screen: the live session is
 * over, could not be found or joined, or the server is refusing it for now.
 * One message, at most one action (retry the session check, or join the
 * session the presenter replaced this one with), and "back to top".
 */
import React from 'react'
import type { FollowerStatus } from '../hooks/useSyncFollower'
import { useI18n } from '../lib/i18n'
import type { EndFrame } from '../lib/sync'

// Why a follower's session ended, by the ``end`` frame's reason; anything
// else (a plain end, or none) reads as "Live session ended".
const FOLLOWER_END_MESSAGE_KEYS: Record<string, string> = {
  presenter_idle: 'sync.sessionEndedIdle',
  max_age: 'sync.sessionEndedMaxAge',
  ended_by_owner: 'sync.sessionEndedByPresenter',
  presenter_gone: 'sync.sessionEndedPresenterGone',
  superseded: 'sync.sessionSuperseded',
}

// The message for each terminal status other than 'ended' (which is worded
// by its end reason).
const FOLLOWER_STATUS_MESSAGE_KEYS: Partial<Record<FollowerStatus, string>> = {
  notfound: 'sync.sessionNotFound',
  error: 'sync.sessionPeekError',
  full: 'sync.roomFull',
  unauthorized: 'sync.sessionUnauthorized',
  forbidden: 'sync.accessBlocked',
}

// The overlay's one action besides "back to top": a button, or a link (a full
// page load, so the next session starts from its own peek).
export type FollowerOverlayAction =
  | { label: string; onClick: () => void; href?: undefined }
  | { label: string; href: string; onClick?: undefined }

/** What the overlay says, and offers, for a follower in ``status``. */
export function followerOverlayContent(
  status: FollowerStatus,
  endedReason: string | null,
  endedNext: EndFrame['next'] | null,
  t: (key: string, params?: Record<string, string | number>) => string,
  retryPeek: () => void,
): { message: string; action?: FollowerOverlayAction } {
  const key = status === 'ended'
    ? FOLLOWER_END_MESSAGE_KEYS[endedReason ?? ''] ?? 'sync.sessionEnded'
    : FOLLOWER_STATUS_MESSAGE_KEYS[status] ?? 'sync.sessionEnded'
  const action: FollowerOverlayAction | undefined = status === 'error'
    ? { label: t('sync.retry'), onClick: retryPeek }
    : status === 'ended' && endedNext
      ? {
          label: t('sync.joinNextSession'),
          href: `/slides/${encodeURIComponent(endedNext.slide_public_id)}?sync=${encodeURIComponent(endedNext.sid)}`,
        }
      : undefined
  return { message: t(key), action }
}

export default function FollowerEndedOverlay({ message, action }: { message: string; action?: FollowerOverlayAction }) {
  const { t } = useI18n()
  return (
    <div style={{
      position: 'absolute', inset: 0, zIndex: 50,
      background: 'rgba(0,0,0,0.45)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    }}>
      <div style={{
        background: '#fff', padding: '20px 28px', borderRadius: 10,
        boxShadow: '0 10px 30px rgba(0,0,0,0.25)', maxWidth: 320,
        textAlign: 'center', fontSize: 14, fontWeight: 600,
      }}>
        {message}
        {action && (
          <div style={{ marginTop: 12 }}>
            {action.href !== undefined ? (
              <a href={action.href} style={followerActionStyle}>{action.label}</a>
            ) : (
              <button type="button" onClick={action.onClick} style={followerActionStyle}>{action.label}</button>
            )}
          </div>
        )}
        <div style={{ marginTop: 12 }}>
          <a href="/" style={{ color: '#1d4ed8', fontSize: 13 }}>{t('viewer.backToTop')}</a>
        </div>
      </div>
    </div>
  )
}

const followerActionStyle: React.CSSProperties = {
  display: 'inline-block',
  background: '#1d4ed8',
  color: '#fff',
  border: '1px solid #1d4ed8',
  borderRadius: 6,
  padding: '6px 14px',
  fontSize: 13,
  fontWeight: 600,
  cursor: 'pointer',
  textDecoration: 'none',
}
