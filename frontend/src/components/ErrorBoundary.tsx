import React from 'react'
import { useI18n } from '../lib/i18n'
import { safeSessionGetItem, safeSessionSetItem } from '../lib/safeStorage'
import { STORAGE_KEYS } from '../lib/storageKeys'

// A lazy route chunk that no longer exists (the server was redeployed while
// this tab stayed open) rejects its dynamic import. Without a boundary that
// rejection unmounts the whole React tree and leaves a blank page. The fix is
// a reload, which fetches the new index.html and its current chunk names.
//
// An automatic reload is allowed at most once per RELOAD_COOLDOWN_MS: a chunk
// that keeps failing (a gateway timeout, a deploy that really dropped it) then
// ends in the fallback screen instead of an endless reload loop - however late
// after page load the failure happens. A later, genuinely new redeploy (past
// the cooldown) can still reload once more.
const RELOAD_FLAG = STORAGE_KEYS.chunkReload
export const RELOAD_COOLDOWN_MS = 5 * 60_000

export function isChunkLoadError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? '')
  return (
    /Failed to fetch dynamically imported module/i.test(message) ||
    /error loading dynamically imported module/i.test(message) ||
    /Importing a module script failed/i.test(message) ||
    /Loading chunk [\w-]+ failed/i.test(message)
  )
}

// The time of the last automatic reload lives in sessionStorage, with
// window.name as a fallback: window.name survives a reload of the same tab
// without any storage, so a browser that blocks storage (where safeStorage
// degrades to no-ops) cannot turn a missing chunk into a reload loop.
const NAME_MARK = 'wsi-chunk-reloaded'
const NAME_MARK_RE = new RegExp(`${NAME_MARK}=(\\d+)`)

function lastReloadAt(): number {
  let last = Number(safeSessionGetItem(RELOAD_FLAG))
  if (!Number.isFinite(last)) last = 0
  if (typeof window !== 'undefined') {
    const m = NAME_MARK_RE.exec(window.name)
    if (m) last = Math.max(last, Number(m[1]))
  }
  return last
}

function markReloaded(now: number): void {
  safeSessionSetItem(RELOAD_FLAG, String(now))
  // Storage that is blocked - or that refuses this write while still holding
  // an older value - does not carry the mark; fall back to window.name.
  if (safeSessionGetItem(RELOAD_FLAG) !== String(now)) {
    const without = window.name.replace(new RegExp(`\\s*${NAME_MARK}=\\d+`), '')
    window.name = `${without} ${NAME_MARK}=${now}`.trim()
  }
}

/** Reload, unless one already happened within RELOAD_COOLDOWN_MS; returns
 *  whether it did. */
export function reloadOnceForStaleChunk(): boolean {
  const now = Date.now()
  if (now - lastReloadAt() < RELOAD_COOLDOWN_MS) return false
  markReloaded(now)
  window.location.reload()
  return true
}

/** ``vite:preloadError`` listener: reload (once per cooldown) for a missing /
 *  stale chunk. The event is swallowed only when that reload really happens;
 *  otherwise - and for any other failure - it is left to propagate, so the
 *  original error stays visible and the ErrorBoundary shows its fallback. */
export function handlePreloadError(event: Event): void {
  const payload = (event as Event & { payload?: unknown }).payload
  const message = payload instanceof Error ? payload.message : String(payload ?? '')
  if (isChunkLoadError(payload) || /Unable to preload/i.test(message)) {
    if (reloadOnceForStaleChunk()) event.preventDefault()
    return
  }
  console.error('vite:preloadError (not a missing chunk, no reload):', payload)
}

function Fallback() {
  const { t } = useI18n()
  return (
    <div role="alert" style={{ padding: 24, textAlign: 'center' }}>
      <p>{t('common.appError')}</p>
      <button type="button" onClick={() => window.location.reload()}>
        {t('common.appErrorReload')}
      </button>
    </div>
  )
}

interface State { failed: boolean }

export default class ErrorBoundary extends React.Component<{ children: React.ReactNode }, State> {
  state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  componentDidCatch(error: unknown) {
    if (isChunkLoadError(error)) reloadOnceForStaleChunk()
  }

  render() {
    return this.state.failed ? <Fallback /> : this.props.children
  }
}
