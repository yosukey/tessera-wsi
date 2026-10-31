/**
 * Live-sync discovery channel — lightweight presence WebSocket.
 *
 * Receives a snapshot of currently active sessions on connect, then live
 * started/ended/slide_changed events. Designed to be cheap on idle: the
 * browser only opens the socket when the page is foregrounded
 * (visibilitychange triggers close/reopen) so background tabs don't hold
 * a connection open.
 *
 * The SPA mounts this only on the IndexPage — importing from elsewhere
 * is intentional misuse.
 */

import { logger } from './logger'
import { backoffWithJitter } from './sync'

// The slide's title and catalog id, sent with every entry and event, so the
// banner can name a slide the index page's loaded listing does not hold.
export type DiscoverySlideLabels = {
  slide_title?: string | null
  slide_catalog_id?: string | null
}

export type DiscoverySessionEntry = DiscoverySlideLabels & {
  sid: string
  slide_id: number
  // The slide's public_id token — what the "join" banner navigates to.
  slide_public_id: string
}

export type DiscoveryEvent =
  | { type: 'snapshot'; sessions: DiscoverySessionEntry[] }
  | ({ type: 'started' } & DiscoverySessionEntry)
  | { type: 'ended'; sid: string }
  | ({ type: 'slide_changed' } & DiscoverySessionEntry)

const PING_INTERVAL_MS = 30_000
const PING_TIMEOUT_MS = 60_000
// How long the last list stands once the channel is down (a drop, a hidden
// tab, an outage) before it is cleared — reported as an empty snapshot — so the
// banner stops offering sessions that may have ended meanwhile. Long enough to
// ride out a reconnect, after which a fresh snapshot replaces the list anyway.
const STALE_AFTER_MS = 10_000

function discoveryUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/api/sync/discovery`
}

export type DiscoverySocketOpts = {
  onEvent: (e: DiscoveryEvent) => void
}

export class DiscoverySocket {
  private ws: WebSocket | null = null
  private closed = false
  private reconnectIdx = 0
  private pingTimer: ReturnType<typeof setInterval> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private lastInbound = 0
  private visListener: (() => void) | null = null
  private onlineListener: (() => void) | null = null
  private staleTimer: ReturnType<typeof setTimeout> | null = null

  // Instantiating a DiscoverySocket has the side effect of opening
  // the WebSocket and registering a visibilitychange listener. Tests and
  // SSR-style environments that want to construct the object without
  // touching the network should treat this as a "live" object — call
  // close() in cleanup.
  constructor(private opts: DiscoverySocketOpts) {
    this.visListener = () => this.onVisibilityChange()
    document.addEventListener('visibilitychange', this.visListener)
    // Back online: a reconnect waiting out its backoff goes soon — not at
    // once, so a class whose access point came back does not arrive together.
    this.onlineListener = () => {
      if (this.closed || this.reconnectTimer == null || document.visibilityState === 'hidden') return
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.open() }, backoffWithJitter(0))
    }
    window.addEventListener('online', this.onlineListener)
    if (document.visibilityState !== 'hidden') this.open()
  }

  close() {
    this.closed = true
    if (this.visListener) {
      document.removeEventListener('visibilitychange', this.visListener)
      this.visListener = null
    }
    if (this.onlineListener) {
      window.removeEventListener('online', this.onlineListener)
      this.onlineListener = null
    }
    this.cancelStale()
    this.stopPing()
    if (this.reconnectTimer != null) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
    try { this.ws?.close() } catch { /* swallow */ }
    this.ws = null
  }

  private onVisibilityChange() {
    if (this.closed) return
    if (document.visibilityState === 'hidden') {
      // Drop the connection entirely so background tabs cost nothing. Cancel
      // any pending reconnect too (mirroring close()), otherwise a timer armed
      // by a prior disconnect keeps ticking and re-opens the socket in the
      // background — or fires just after the page re-foregrounds and opens a second
      // one on top of the fresh connection below.
      this.stopPing()
      if (this.reconnectTimer != null) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
      try { this.ws?.close() } catch { /* swallow */ }
      this.ws = null
      this.armStale()
      return
    }
    // A socket silent past the timeout (the hidden tab's timers were
    // throttled, so the ping check may not have run) is replaced.
    if (this.ws && Date.now() - this.lastInbound > PING_TIMEOUT_MS) this.dropSocket()
    // Reopen on focus; the snapshot will resync state. Clear any pending
    // reconnect first so it can't fire *after* the open() below and clobber
    // this.ws with a duplicate live socket (leaking the first).
    if (this.reconnectTimer != null) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
    if (!this.ws) this.open()
  }

  private open() {
    if (this.closed) return
    // Never stack a second socket on top of a live one. A reconnect timer that
    // fires after a visibilitychange-driven reopen (or vice versa) would
    // otherwise overwrite this.ws, orphaning the previous socket — its onclose
    // guard (`this.ws !== ws`) then leaves it OPEN indefinitely, so it keeps
    // its onmessage handler (duplicate DiscoveryEvent delivery) and holds a
    // second server-side subscriber slot.
    if (this.ws) return
    const ws = new WebSocket(discoveryUrl())
    this.ws = ws
    this.lastInbound = Date.now()
    // Whether this socket ever delivered a frame: an upgrade that never got
    // through closes with 1006 without one.
    let gotFrame = false

    ws.onopen = () => {
      // reconnectIdx is NOT reset here: a full server (4503) accepts the
      // upgrade and closes right away, so resetting on open would pin the
      // retry at the first 500 ms step. It resets on the first frame instead —
      // the snapshot a successful subscribe always sends.
      this.startPing()
    }

    ws.onmessage = (ev) => {
      this.lastInbound = Date.now()
      this.reconnectIdx = 0
      gotFrame = true
      // The first frame is the snapshot, which replaces the stale list.
      this.cancelStale()
      let msg: unknown
      try {
        msg = JSON.parse(typeof ev.data === 'string' ? ev.data : '')
      } catch {
        return
      }
      if (!msg || typeof msg !== 'object') return
      const t = (msg as { type?: string }).type
      if (t === 'ping') {
        this.send({ type: 'pong' })
        return
      }
      if (t === 'pong') return
      this.opts.onEvent(msg as DiscoveryEvent)
    }

    ws.onclose = (ev) => {
      // Ignore a close event from a socket already replaced. A
      // visibilitychange (hidden -> close old, visible -> open new) can leave
      // the *old* socket's onclose to fire late; without this guard it would
      // stopPing() the new socket, null out this.ws (orphaning it), emit a
      // spurious 'closed', and schedule a reconnect that opens a third socket.
      if (this.ws !== ws) return
      // Surface upgrade failures (502 from nginx without ws_upgrade
      // headers, vite proxy without ws:true, etc.) so operators can
      // diagnose without inspecting raw network traffic. Only for a socket
      // that never got a frame through, and only on the first attempt: a 1006
      // ending a healthy session is a dropped connection or a restarting
      // server, not a proxy misconfiguration, and the retries of an outage
      // would repeat the warning on every backoff step.
      if (!gotFrame && this.reconnectIdx === 0 && ev.code === 1006) {
        logger.warn(
          '[wsi-sync] discovery WebSocket upgrade failed — verify nginx ' +
          '(/api/sync/discovery -> wsi-sync upstream with Upgrade headers) ' +
          'or vite proxy (ws: true on /api).',
        )
      }
      this.stopPing()
      this.ws = null
      if (this.closed) return
      this.armStale()
      // Permanent close codes — retrying would just keep producing the same
      // failure on every backoff tick. Mirrors the stop-list in lib/sync.ts:
      //   4401 / 4403 — _ws_guard rejected the upgrade (auth / IP filter /
      //                 another site's page)
      // Discovery is best-effort presence info; the rest of the app keeps
      // working without it, so a silent stop is preferable to a reconnect
      // storm that floods the server's access log.
      // Deliberately NOT on this list:
      //   4429 (CLOSE_RATE_LIMITED) — the per-IP login window clears itself.
      //   1013 (CLOSE_TRY_AGAIN_LATER) — the server could not check the
      //        credential (Cloudflare Access keys or its DB unreachable).
      //   4503 (subscriber cap, global or per-IP) — slots free up as other
      //        tabs close, and one host holding the whole channel must not
      //        switch the banner off for good on every real viewer. The
      //        backoff below grows to 30 s since no snapshot arrives to reset it.
      if (ev.code === 4401 || ev.code === 4403) {
        this.closed = true
        // Nothing will replace the list any more: clear it now.
        this.cancelStale()
        this.opts.onEvent({ type: 'snapshot', sessions: [] })
        return
      }
      if (document.visibilityState === 'hidden') return
      const delay = backoffWithJitter(this.reconnectIdx)
      this.reconnectIdx += 1
      this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.open() }, delay)
    }
  }

  private send(msg: object) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    try { this.ws.send(JSON.stringify(msg)) } catch { /* swallow */ }
  }

  private startPing() {
    this.stopPing()
    this.pingTimer = setInterval(() => {
      if (Date.now() - this.lastInbound > PING_TIMEOUT_MS) {
        this.dropSocket()
        return
      }
      this.send({ type: 'ping' })
    }, PING_INTERVAL_MS)
  }

  // Give up on a silent socket and reconnect now, without waiting for its
  // close: on a dead path a close() waits for the closing handshake (up to a
  // minute in Chromium) before onclose fires. Its handlers are detached first,
  // so nothing from it arrives afterwards.
  private dropSocket() {
    const ws = this.ws
    if (!ws) return
    ws.onopen = null
    ws.onmessage = null
    ws.onclose = null
    ws.onerror = null
    try { ws.close() } catch { /* swallow */ }
    this.stopPing()
    this.ws = null
    if (this.closed) return
    this.armStale()
    if (document.visibilityState === 'hidden') return
    const delay = backoffWithJitter(this.reconnectIdx)
    this.reconnectIdx += 1
    this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.open() }, delay)
  }

  private armStale() {
    if (this.staleTimer != null || this.closed) return
    this.staleTimer = setTimeout(() => {
      this.staleTimer = null
      if (!this.closed) this.opts.onEvent({ type: 'snapshot', sessions: [] })
    }, STALE_AFTER_MS)
  }

  private cancelStale() {
    if (this.staleTimer != null) { clearTimeout(this.staleTimer); this.staleTimer = null }
  }

  private stopPing() {
    if (this.pingTimer != null) {
      clearInterval(this.pingTimer)
      this.pingTimer = null
    }
  }
}
