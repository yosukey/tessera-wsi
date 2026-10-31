/**
 * SyncPresenterSidebar — right-rail dock shown while a live-sync
 * session is active.
 *
 * Two states: ``expanded`` (320 px) holds the full session card
 * — live banner, follower URL + QR + copy, audience badge, audience
 * list, CSV export, end button — and ``collapsed`` (40 px) shows just
 * a chevron + viewer-count badge + end button so the OSD canvas keeps
 * almost full width.
 *
 * The collapsed/expanded preference is persisted in localStorage so a
 * presenter who folded the panel last time gets it folded again.
 *
 * Calls ``onCollapseChange`` whenever the width changes so the parent
 * can re-sync OpenSeadragon's canvas pixel buffer to the new container
 * size. Fired from a layout effect so the redraw happens before the
 * browser paints the new flex layout.
 */
import { useCallback, useLayoutEffect, useEffect, useMemo, useRef, useState } from 'react'
import QRCode from 'qrcode'
import { useI18n } from '../lib/i18n'
import { copyTextToClipboard, triggerDownload } from '../lib/clipboard'
import { formatElapsed } from '../lib/elapsed'
import { STORAGE_KEYS } from '../lib/storageKeys'
import type { CreatedSyncSession } from '../lib/api'
import type { AudienceFrame, ConnState, FollowerInfo } from '../lib/sync'

const COLLAPSE_STORAGE_KEY = STORAGE_KEYS.syncPresenterSidebarCollapsed
export const SIDEBAR_WIDTH_EXPANDED = 320
export const SIDEBAR_WIDTH_COLLAPSED = 40

export type SyncPresenterSidebarProps = {
  session: CreatedSyncSession
  audience: AudienceFrame
  connState: ConnState
  onStop: () => void
  /** Fired with the new width whenever the sidebar collapses/expands. */
  onCollapseChange?: (collapsed: boolean) => void
}

function readCollapsedPref(): boolean {
  try {
    return localStorage.getItem(COLLAPSE_STORAGE_KEY) === '1'
  } catch {
    return false
  }
}
function writeCollapsedPref(collapsed: boolean): void {
  try {
    localStorage.setItem(COLLAPSE_STORAGE_KEY, collapsed ? '1' : '0')
  } catch { /* ignore */ }
}

export default function SyncPresenterSidebar({
  session,
  audience,
  connState,
  onStop,
  onCollapseChange,
}: SyncPresenterSidebarProps) {
  const { t } = useI18n()
  const [collapsed, setCollapsed] = useState<boolean>(() => readCollapsedPref())
  const [paneOpen, setPaneOpen] = useState(true)
  const [sortMode, setSortMode] = useState<'joined' | 'name'>('joined')
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle')
  const qrRef = useRef<HTMLCanvasElement | null>(null)
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => { if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current) }, [])

  // Render QR only while expanded — the canvas isn't in the tree otherwise.
  useEffect(() => {
    if (collapsed) return
    const c = qrRef.current
    if (!c) return
    QRCode.toCanvas(c, session.follower_url, { width: 144, margin: 2 })
      .catch(() => { /* swallow */ })
  }, [session.follower_url, collapsed])

  // Notify the parent on every collapse-state change so it can re-sync
  // OSD's canvas to the new container width. useLayoutEffect so the
  // redraw lands in the same paint as the new flex layout.
  useLayoutEffect(() => {
    onCollapseChange?.(collapsed)
  }, [collapsed, onCollapseChange])

  const toggleCollapsed = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev
      writeCollapsedPref(next)
      return next
    })
  }, [])

  // Confirm before tearing the session down — the End button is large,
  // red, and on the same panel as the collapse chevron, so a single
  // accidental click would otherwise drop every viewer.
  const handleStop = useCallback(() => {
    const ok = window.confirm(t('sync.confirmStop') as string)
    if (ok) onStop()
  }, [onStop, t])

  const copy = useCallback(async () => {
    if (copyTimerRef.current !== null) { clearTimeout(copyTimerRef.current); copyTimerRef.current = null }
    try {
      await copyTextToClipboard(session.follower_url)
      setCopyStatus('copied')
      copyTimerRef.current = setTimeout(() => { copyTimerRef.current = null; setCopyStatus('idle') }, 1500)
    } catch {
      setCopyStatus('failed')
      copyTimerRef.current = setTimeout(() => { copyTimerRef.current = null; setCopyStatus('idle') }, 2000)
    }
  }, [session.follower_url])

  const sortedFollowers = useMemo(() => {
    const list = [...audience.followers]
    if (sortMode === 'name') {
      return list.sort((a, b) =>
        (a.name || '').localeCompare(b.name || '')
        || a.joined_at - b.joined_at,
      )
    }
    return list.sort((a, b) => a.joined_at - b.joined_at)
  }, [audience.followers, sortMode])

  // Single 30 s "now" tick at the sidebar so the joined-since label on every
  // FollowerRow updates from one shared timer. Letting each row own its
  // own setInterval would, under the FOLLOWERS_PER_ROOM_MAX cap (200), mean up
  // to 200 concurrent intervals and 200 individual setState calls per tick.
  const now = useNow()

  const pseudonymous = audience.pseudonymous === true
  const downloadCsv = useCallback(() => {
    const rows = [
      // Under the "hashed" privacy mode the column holds pseudonyms, not
      // addresses; say so in the header the file carries away with it.
      ['name', pseudonymous ? 'ip_pseudonym' : 'ip', 'ua', 'hostname', 'joined_at_iso', 'visible', 'viewport_w', 'viewport_h'],
      ...sortedFollowers.map((f) => [
        f.name ?? '',
        f.ip,
        f.ua,
        f.hostname ?? '',
        new Date(f.joined_at * 1000).toISOString(),
        String(f.visible),
        f.vw == null ? '' : String(f.vw),
        f.vh == null ? '' : String(f.vh),
      ]),
    ]
    const csv = rows.map((row) => row.map(csvEscape).join(',')).join('\r\n')
    // UTF-8 BOM so Excel (notably on Japanese-locale Windows) decodes names as UTF-8.
    const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    triggerDownload(url, `wsi-sync-${session.session_id}.csv`)
    // Defer revoke so any browser that initiates the download asynchronously
    // after click() returns still has a valid blob URL. Synchronous revoke
    // races the download in some browsers and silently produces empty files.
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }, [sortedFollowers, session.session_id, pseudonymous])

  // -------------------------------------------------- collapsed strip ----
  if (collapsed) {
    return (
      <aside style={collapsedRoot} aria-label={t('sync.modalTitle') as string}>
        <button
          type="button"
          onClick={toggleCollapsed}
          title={t('sync.expand') as string}
          aria-label={t('sync.expand') as string}
          style={collapsedChevronStyle}
        >
          ‹
        </button>
        <span style={collapsedCountStyle(audience.count)}>
          {audience.count}
        </span>
        <button
          type="button"
          onClick={handleStop}
          title={t('sync.stop') as string}
          aria-label={t('sync.stop') as string}
          style={collapsedEndStyle}
        >
          ■
        </button>
      </aside>
    )
  }

  // -------------------------------------------------- expanded panel -----
  return (
    <aside className="overscroll-panel" style={expandedRoot} aria-label={t('sync.modalTitle') as string}>
      <div style={liveBannerStyle}>
        <span style={liveDotStyle} />
        <span style={{ fontSize: 12, fontWeight: 600 }}>
          {t('sync.live')}
        </span>
        <button type="button" onClick={handleStop} style={endBtnInlineStyle}>
          {t('sync.stop')}
        </button>
      </div>

      <div style={modalHeaderRow}>
        <strong style={{ fontSize: 14 }}>{t('sync.modalTitle')}</strong>
        <span style={headerRightGroupStyle}>
          <span style={connBadgeStyle(connState)}>{connStateLabel(connState, t)}</span>
          <button
            type="button"
            onClick={toggleCollapsed}
            title={t('sync.collapse') as string}
            aria-label={t('sync.collapse') as string}
            style={collapseBtnStyle}
          >
            ›
          </button>
        </span>
      </div>
      <div style={{ fontSize: 12, color: '#475569', marginBottom: 8 }}>
        {t('sync.modalIntro')}
      </div>
      <div style={{ fontSize: 11, color: '#1d4ed8', marginBottom: 10 }}>
        {t('sync.lanHint')}
      </div>

      <div style={qrRowStyle}>
        <canvas
          ref={qrRef}
          style={{ width: 144, height: 144, borderRadius: 6, background: '#fff', flexShrink: 0 }}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 11, color: '#64748b' }}>
            {t('sync.urlLabel')}
          </div>
          <div style={urlBoxStyle}>{session.follower_url}</div>
          <button type="button" onClick={copy} style={copyBtnStyle}>
            {copyStatus === 'copied'
              ? t('sync.urlCopied')
              : copyStatus === 'failed'
                ? t('sync.copyFailed')
                : t('sync.copyUrl')}
          </button>
        </div>
      </div>

      <div style={audienceBarStyle}>
        <span style={audienceBadgeStyle(audience.count)}>
          🟢 {t('sync.audienceBadge', { count: audience.count })}
        </span>
        <button
          type="button"
          onClick={() => setPaneOpen((v) => !v)}
          style={smallBtnStyle}
        >
          {paneOpen ? t('sync.audiencePanelClose') : t('sync.audiencePanelOpen')}
        </button>
      </div>

      {paneOpen && (
        <div style={panelStyle}>
          <div style={panelTopRow}>
            <button
              type="button"
              onClick={() => setSortMode((m) => (m === 'joined' ? 'name' : 'joined'))}
              style={smallBtnStyle}
            >
              {sortMode === 'joined' ? t('sync.audienceSortJoined') : t('sync.audienceSortName')}
            </button>
            <button type="button" onClick={downloadCsv} style={smallBtnStyle}>
              {t('sync.audienceExportCsv')}
            </button>
          </div>
          {pseudonymous && (
            <div style={{ padding: '6px 10px', fontSize: 11, color: '#64748b', borderBottom: '1px solid #f1f5f9' }}>
              {t('sync.audiencePseudonymous')}
            </div>
          )}
          {sortedFollowers.length === 0 ? (
            <div style={{ padding: 8, fontSize: 12, color: '#64748b' }}>
              {t('sync.audienceEmpty')}
            </div>
          ) : (
            <ul className="overscroll-panel" style={panelListStyle}>
              {sortedFollowers.map((f) => (
                <FollowerRow key={f.id} follower={f} now={now} />
              ))}
            </ul>
          )}
        </div>
      )}
    </aside>
  )
}

// ----- follower row ------------------------------------------------------

function FollowerRow({ follower, now }: { follower: FollowerInfo; now: number }) {
  const { t } = useI18n()
  const elapsed = useMemo(
    () => formatElapsed(now / 1000 - follower.joined_at, t),
    [follower.joined_at, now, t],
  )
  const viewport = formatViewport(follower.vw, follower.vh)
  return (
    <li style={panelRowStyle(follower.visible)}>
      <span style={visibleDotStyle(follower.visible)} />
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ fontSize: 13, fontWeight: 600 }}>
          {follower.name || t('sync.audienceAnonymous')}
        </div>
        <div style={{ fontSize: 11, color: '#64748b', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {followerMeta(follower, t('sync.audienceBrowserUnknown'))}
        </div>
        {/* Join-time viewer size, on its own line. Appending it to the line
            above would put it last in a nowrap+ellipsis run only ~227px wide:
            an IPv4 + UA + hostname row already overflows on its own, so the
            size would be clipped away entirely — or, worse, clipped mid-number
            ("1194" rendering as "11") on a row that merely has an IPv6
            address. Omitted for followers that reported no size. */}
        {viewport && (
          <div style={{ fontSize: 11, color: '#94a3b8', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {viewport}
          </div>
        )}
      </div>
      <div style={{ fontSize: 11, color: '#94a3b8', flexShrink: 0 }}>{elapsed}</div>
    </li>
  )
}

/**
 * `203.0.113.7 — Chrome 128 / Windows (pc1)`. Under the "hashed" privacy mode
 * the server sends a pseudonym for the address and no UA / hostname, so the
 * missing parts are left out rather than rendered as a bare "—".
 */
function followerMeta(follower: FollowerInfo, unknownBrowser: string): string {
  const parts = [follower.ip, follower.ua ? summariseUserAgent(follower.ua, unknownBrowser) : ''].filter(Boolean)
  return (parts.length ? parts.join(' — ') : '—') + (follower.hostname ? ` (${follower.hostname})` : '')
}

/** `1194×834`, or '' when the follower reported no usable size. */
function formatViewport(vw: number | null, vh: number | null): string {
  if (!vw || !vh) return ''
  return `${vw}×${vh}`
}

function useNow() {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(id)
  }, [])
  return now
}

// ``unknownBrowser`` names a browser the patterns below do not recognise.
function summariseUserAgent(ua: string, unknownBrowser: string): string {
  if (!ua) return '—'
  let m: RegExpExecArray | null
  const browser =
    (m = /Edg\/([0-9]+)/.exec(ua)) ? `Edge ${m[1]}` :
    (m = /Chrome\/([0-9]+)/.exec(ua)) ? `Chrome ${m[1]}` :
    (m = /Firefox\/([0-9]+)/.exec(ua)) ? `Firefox ${m[1]}` :
    (/Safari\/[0-9]+/.test(ua) && (m = /Version\/([0-9.]+)/.exec(ua))) ? `Safari ${m[1]}` :
    unknownBrowser
  const os =
    /Windows NT ([0-9.]+)/.exec(ua) ? 'Windows' :
    /Mac OS X/.exec(ua) ? 'macOS' :
    /Android/.exec(ua) ? 'Android' :
    /iPhone|iPad|iOS/.exec(ua) ? 'iOS' :
    /Linux/.exec(ua) ? 'Linux' :
    ''
  return os ? `${browser} / ${os}` : browser
}

function csvEscape(v: string): string {
  // Defuse CSV-formula injection: a name / hostname starting with =, +, -,
  // @, TAB, or CR is interpreted as a formula by Excel / Google Sheets
  // when the exported file is opened. The TAB/CR cases are the OWASP CSV-
  // injection cheat-sheet's "leading whitespace" entries — they're
  // already stripped server-side by sanitize_name / _scrub_metadata for
  // name/hostname/ua, but the ip field is forwarded verbatim from the
  // proxy header, so this is still the right defensive layer to keep.
  // Prepending a single quote forces the cell to render as text. Quote-
  // wrap only when the cleaned value contains characters CSV would
  // otherwise mis-parse.
  let out = v
  if (/^[=+\-@\t\r]/.test(out)) out = `'${out}`
  if (/[,"\r\n]/.test(out)) return `"${out.replace(/"/g, '""')}"`
  return out
}

function connStateLabel(s: ConnState, t: ReturnType<typeof useI18n>['t']): string {
  if (s === 'open') return ''
  if (s === 'connecting') return t('sync.connecting') as string
  if (s === 'unauthorized') return t('sync.unauthorized') as string
  if (s === 'rejected') return t('sync.presenterRejected') as string
  if (s === 'forbidden') return t('sync.accessBlocked') as string
  if (s === 'notOwner') return t('sync.notOwner') as string
  return t('sync.disconnected') as string
}

// ---------- styles ------------------------------------------------------

const expandedRoot: React.CSSProperties = {
  width: SIDEBAR_WIDTH_EXPANDED,
  flexShrink: 0,
  height: '100%',
  background: '#fff',
  borderLeft: '1px solid var(--color-border, #ccc)',
  boxShadow: '-4px 0 12px rgba(0, 0, 0, 0.06)',
  padding: 12,
  overflowY: 'auto',
  display: 'flex',
  flexDirection: 'column',
  gap: 0,
}

const collapsedRoot: React.CSSProperties = {
  width: SIDEBAR_WIDTH_COLLAPSED,
  flexShrink: 0,
  height: '100%',
  background: '#fff',
  borderLeft: '1px solid var(--color-border, #ccc)',
  boxShadow: '-4px 0 12px rgba(0, 0, 0, 0.06)',
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  gap: 12,
  padding: '10px 0',
}

const collapsedChevronStyle: React.CSSProperties = {
  background: 'transparent',
  border: '1px solid #cbd5e1',
  borderRadius: 4,
  width: 26,
  height: 26,
  fontSize: 14,
  fontWeight: 700,
  color: '#374151',
  cursor: 'pointer',
  lineHeight: 1,
}

function collapsedCountStyle(count: number): React.CSSProperties {
  return {
    fontSize: 12,
    fontWeight: 700,
    padding: '6px 0',
    background: count > 0 ? '#dcfce7' : '#f3f4f6',
    color: count > 0 ? '#166534' : '#475569',
    borderRadius: 999,
    width: 26,
    textAlign: 'center',
    boxShadow: count > 0 ? '0 0 0 2px rgba(34, 197, 94, 0.18)' : undefined,
  }
}

const collapsedEndStyle: React.CSSProperties = {
  marginTop: 'auto',
  background: '#dc2626',
  color: '#fff',
  border: '1px solid #b91c1c',
  borderRadius: 4,
  width: 26,
  height: 26,
  fontSize: 12,
  fontWeight: 700,
  cursor: 'pointer',
  lineHeight: 1,
}

const liveBannerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  background: '#fef2f2',
  border: '1px solid #fecaca',
  borderRadius: 6,
  padding: '6px 10px',
  marginBottom: 10,
}

const liveDotStyle: React.CSSProperties = {
  width: 10,
  height: 10,
  borderRadius: '50%',
  background: '#dc2626',
  boxShadow: '0 0 8px rgba(220, 38, 38, 0.7)',
  flexShrink: 0,
}

const headerRightGroupStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
}

const collapseBtnStyle: React.CSSProperties = {
  background: 'transparent',
  border: '1px solid #cbd5e1',
  borderRadius: 4,
  padding: '2px 8px',
  fontSize: 14,
  fontWeight: 700,
  color: '#374151',
  cursor: 'pointer',
  lineHeight: 1,
}

const endBtnInlineStyle: React.CSSProperties = {
  marginLeft: 'auto',
  background: '#dc2626',
  color: '#fff',
  border: '1px solid #b91c1c',
  borderRadius: 4,
  padding: '4px 12px',
  fontSize: 12,
  fontWeight: 600,
  cursor: 'pointer',
}

const modalHeaderRow: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  marginBottom: 6,
}

function connBadgeStyle(s: ConnState): React.CSSProperties {
  if (s === 'open') return { display: 'none' }
  return {
    fontSize: 10,
    padding: '2px 6px',
    borderRadius: 999,
    background: '#fef3c7',
    color: '#92400e',
  }
}

const qrRowStyle: React.CSSProperties = {
  display: 'flex',
  gap: 12,
  alignItems: 'flex-start',
}

const urlBoxStyle: React.CSSProperties = {
  fontSize: 11,
  fontFamily: 'monospace',
  background: '#f3f4f6',
  padding: '6px 8px',
  borderRadius: 4,
  wordBreak: 'break-all',
  marginTop: 2,
  marginBottom: 6,
}

const copyBtnStyle: React.CSSProperties = {
  background: '#1d4ed8',
  color: '#fff',
  border: '1px solid #1d4ed8',
  borderRadius: 4,
  padding: '4px 10px',
  fontSize: 12,
  fontWeight: 600,
  cursor: 'pointer',
}

const audienceBarStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  marginTop: 12,
  flexWrap: 'wrap',
}

function audienceBadgeStyle(count: number): React.CSSProperties {
  return {
    fontSize: 12,
    fontWeight: 600,
    padding: '4px 10px',
    background: count > 0 ? '#dcfce7' : '#f3f4f6',
    color: count > 0 ? '#166534' : '#475569',
    borderRadius: 999,
  }
}

const smallBtnStyle: React.CSSProperties = {
  background: 'transparent',
  border: '1px solid #cbd5e1',
  borderRadius: 4,
  padding: '4px 8px',
  fontSize: 11,
  cursor: 'pointer',
  color: '#374151',
}

const panelStyle: React.CSSProperties = {
  marginTop: 8,
  border: '1px solid #e5e7eb',
  borderRadius: 6,
}

const panelTopRow: React.CSSProperties = {
  display: 'flex',
  gap: 6,
  padding: 6,
  borderBottom: '1px solid #e5e7eb',
}

const panelListStyle: React.CSSProperties = {
  listStyle: 'none',
  margin: 0,
  padding: 0,
  maxHeight: 240,
  overflowY: 'auto',
}

function panelRowStyle(visible: boolean): React.CSSProperties {
  return {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    padding: '6px 10px',
    borderBottom: '1px solid #f1f5f9',
    opacity: visible ? 1 : 0.55,
  }
}

function visibleDotStyle(visible: boolean): React.CSSProperties {
  return {
    width: 8,
    height: 8,
    borderRadius: '50%',
    background: visible ? '#22c55e' : '#9ca3af',
    flexShrink: 0,
  }
}
