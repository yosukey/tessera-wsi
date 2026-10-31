/**
 * Live ViewerPage sync — WebSocket client.
 *
 * Two thin wrappers around the same fan-out endpoint at
 * /api/sync/ws/{sid}. Both auto-reconnect with exponential backoff,
 * exchange app-level ping/pong every 15s, and self-close when the peer
 * goes silent for 30s. The frame type definitions below are the schema;
 * docs/operations.md covers the coordinator's deployment shape.
 */

import { makeUid } from './uid'

/**
 * Viewport bounds (x, y, w, h) in OpenSeadragon viewport coordinates.
 * Sending the bounds rect directly lets the follower replay the presenter's
 * frame with a single ``fitBounds`` call — no zoom/aspect math, no
 * accumulated drift between heterogeneous viewport sizes.
 *
 * ``bounds_deg`` carries the rect's own ``Rect.degrees`` (the rotation OSD
 * folds into the bounds rectangle when the viewport is rotated). It MUST be
 * transmitted alongside (x, y, w, h): ``getBounds()`` returns the rotated
 * top-left corner in x/y, so a follower that rebuilds the rect with
 * ``degrees: 0`` would fit the wrong region and zoom whenever the presenter
 * has rotated the slide. ``rotation_deg`` is the viewport rotation
 * (``getRotation()``) applied via ``setRotation`` before the fit; the two are
 * distinct values (``bounds_deg`` is the rect's normalised −rotation).
 * There is no flip: OSD's ``f`` key, which mirrors the view, is off in every
 * viewer (lib/osd.ts attachNoFlipKey).
 */
export type ViewFrame = {
  type: 'view'
  t?: number
  slide_id: number
  // The current slide's unguessable public_id, stamped onto every broadcast
  // frame by the server. Followers navigate/compare by this token (viewer URLs
  // are keyed by public_id); the numeric slide_id stays for internal matching.
  slide_public_id?: string
  bounds: [number, number, number, number]
  bounds_deg?: number
  rotation_deg: number
}

/**
 * Cursor coordinates are in image (level-0) pixels so the follower can map
 * them through OpenSeadragon's image->viewer-element transform regardless
 * of its own zoom/pan state.
 */
export type CursorFrame = {
  type: 'cursor'
  t?: number
  x: number
  y: number
  visible: boolean
}

export type EndFrame = {
  type: 'end'
  reason?: string
  // On a ``superseded`` end: the session the presenter started in its place,
  // when it is on a slide the index page lists (see create_superseding), so
  // the follower can be offered to join it.
  next?: { sid: string; slide_public_id: string }
}

/**
 * Presence signal for the presenter, fanned out to followers when the
 * presenter detaches (``present: false`` with ``grace_seconds`` left before
 * the session is torn down) and again when they reattach (``present: true``).
 * Lets followers surface a "presenter left / ending in Ns" countdown and
 * clear it on return.
 */
export type PresenterFrame = {
  type: 'presenter'
  present: boolean
  grace_seconds?: number
}

export type FollowerInfo = {
  id: string
  name: string | null
  ip: string
  ua: string
  hostname: string | null
  joined_at: number
  visible: boolean
  // Follower's viewer-canvas size in CSS pixels at join time, or null when the
  // client didn't report it (unmeasurable) or the server rejected the value.
  // Reported once in the hello frame and never updated, so a follower that
  // rotates or resizes mid-session keeps its join-time figures.
  vw: number | null
  vh: number | null
}

export type AudienceFrame = {
  type: 'audience'
  count: number
  followers: FollowerInfo[]
  // True while the analytics privacy mode is "hashed": each ``ip`` is then a
  // short keyed pseudonym of the address ("" if it could not be computed),
  // and ``ua`` / ``hostname`` are withheld ("" / null). The server always
  // sends it; only the client's placeholder audience (EMPTY_AUDIENCE) omits it.
  pseudonymous?: boolean
}

// 'unauthorized' is reported when the server closed the WebSocket with 4401
// (credential missing, invalid, expired or revoked). The base reconnect loop
// stops in this state so the UI can prompt a re-login instead of looping
// forever with a stale credential.
//
// 'rejected' is reported on 4400: no usable hello (a follower without the
// name WSI_SYNC_REQUIRE_NAME demands, or a hello that was malformed or did
// not arrive within WSI_SYNC_HELLO_TIMEOUT_SEC). Terminal; the UI says the
// connection could not be set up rather than that a login expired.
//
// 'forbidden' is reported on 4403: the server's IP filter denies this
// client's address, or the upgrade did not come from the app's own origin.
// No credential fixes that, so the loop stops; the UI says access from this
// address is blocked rather than asking for a sign-in.
//
// 'notOwner' is reported on 4409, which only a presenter receives: the
// session still exists but belongs to another staff account. Terminal too.
// (A session that is gone is 4404 / 'gone', never 4409.)
//
// 'gone' is reported when the server closed with 4404 NOT_FOUND — the
// session is permanently absent (e.g. server restart wiped in-memory
// rooms, or the presenter ended the session in a way that didn't deliver
// the ``end`` frame before the close, such as a CDN that drops in-flight
// frames on close). Reconnecting with the same sid would just keep
// producing 4404, so the loop stops; the UI can surface "session ended".
//
// 'full' is reported when the server closed with 4503 — the room has
// reached FOLLOWERS_PER_ROOM_MAX, or this client's IP its per-IP share. It is
// NOT terminal: the loop keeps retrying with backoff (a slot frees whenever
// someone leaves, and one host holding every slot must not lock real followers
// out for good), and 'open' follows once a retry is actually seated.
//
// 'open' itself is reported on the first frame the server sends, never on the
// bare socket open: the server accepts every upgrade before its checks run, so
// a refusal (4503, 4429, 1013, ...) opens the socket too, and reporting 'open'
// then would flash the UI to "connected" and clear state (the follower's
// presenter-absence countdown) the server was never going to re-send. A seated
// presenter is sent its audience snapshot and a seated follower an ``attached``
// frame at once, so neither waits on the presenter to move.
//
// 'replaced' is reported when the server closed with 4000 — a newer presenter
// socket took over this session (another tab/device, or this client's own
// reconnect), or the server refused this client's reconnect because another
// tab/device attached while it was away. The loop stops so two live presenter
// tabs can't ping-pong evicting each other; the UI releases the session locally without ending the
// server-side room the surviving socket now drives. Only presenters can
// receive 4000 (followers are never evicted).
export type ConnState =
  | 'connecting' | 'open' | 'closed' | 'unauthorized' | 'rejected' | 'forbidden' | 'notOwner' | 'gone' | 'full' | 'replaced'

const PING_INTERVAL_MS = 15_000
const PING_TIMEOUT_MS = 30_000
// How long a socket must have stayed admitted for its loss to restart the
// backoff from the first step. Resetting on the first frame would let a socket the
// server admits and then drops again (a send-backlog 1013 on a client that
// cannot keep up) reconnect every 500 ms.
const STABLE_CONNECTION_MS = 10_000
// Reconnect backoff schedule. The cap is 30s so a long server outage does
// not turn into a half-hour-blackout for a re-opened laptop lid.
export const RECONNECT_BACKOFF_MS = [500, 1_000, 2_000, 4_000, 8_000, 16_000, 30_000]
// The presenter must be back inside the server's PRESENTER_GRACE_SECONDS (45 s,
// sync_room.py) or the room is reaped. With the follower schedule the attempts
// land at ≈0.5/1.5/3.5/7.5/15.5/31.5/61.5 s, so a drop of 32–45 s could miss
// the window; capping at 8 s keeps an attempt at least every ~10 s (jitter incl.).
export const PRESENTER_RECONNECT_BACKOFF_MS = [500, 1_000, 2_000, 4_000, 8_000]

function wsUrl(path: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}${path}`
}

// "Full" jitter: anywhere between a tenth of the step and the step itself, so
// a whole class whose shared upstream just bounced (followers and index pages
// alike, often behind one NAT address and so one client to nginx's handshake
// limit) spreads over the step rather than arriving within ±25% of it together.
export function backoffWithJitter(
  idx: number,
  schedule: readonly number[] = RECONNECT_BACKOFF_MS,
): number {
  const base = schedule[Math.min(idx, schedule.length - 1)]
  return Math.round(base * (0.1 + Math.random() * 0.9))
}

abstract class _BaseSync {
  protected ws: WebSocket | null = null
  protected closed = false
  protected reconnectIdx = 0
  protected backoffSchedule: readonly number[] = RECONNECT_BACKOFF_MS
  protected pingTimer: ReturnType<typeof setInterval> | null = null
  protected reconnectTimer: ReturnType<typeof setTimeout> | null = null
  protected lastInbound = 0
  // Date.now() of the current socket's first inbound frame — the proof the
  // server admitted it — or 0 until then (see 'open' in ConnState).
  protected admittedAt = 0
  protected pendingHello: object | null = null
  protected onStateChange?: (s: ConnState) => void
  private wakeListener: (() => void) | null = null

  constructor(protected url: string) {}

  protected open() {
    if (this.closed) return
    this.notify('connecting')
    const ws = new WebSocket(this.url)
    this.ws = ws
    // Back online, or the tab shown again: a reconnect waiting out its
    // backoff goes soon (see wake), and a socket that has been silent past the ping
    // timeout (a background tab's timers are throttled, so the check below may
    // not have run) is replaced. Registered once the first socket exists: a
    // constructor whose WebSocket threw never returns an instance anyone
    // could close(), and would leave the listeners behind.
    if (!this.wakeListener && typeof window !== 'undefined') {
      this.wakeListener = () => this.wake()
      window.addEventListener('online', this.wakeListener)
      document.addEventListener('visibilitychange', this.wakeListener)
    }
    this.lastInbound = Date.now()
    this.admittedAt = 0

    ws.onopen = () => {
      // Neither 'open' nor the backoff reset happens here. The server accepts
      // every upgrade before its checks run, so a refusal it then delivers as
      // a close (4429 rate limited, 4503 full, 1008 / 1013) still fires onopen
      // first; resetting here would pin those retries at the first 500 ms step.
      // 'open' waits for the first inbound frame (see onmessage), and the
      // backoff resets only once a socket has stayed up for
      // STABLE_CONNECTION_MS (see handleClose).
      //
      // Subclasses can refresh ``pendingHello`` here (e.g. SyncFollower
      // re-reads the current document.visibilityState) so every hello —
      // including reconnect helloes — carries fresh state.
      this.onSocketOpen()
      // Send the hello frame immediately so the server can authenticate /
      // register the connection. The subclass populates pendingHello
      // before each open.
      if (this.pendingHello) ws.send(JSON.stringify(this.pendingHello))
      // Subclasses can replay sticky post-hello state here — used by
      // SyncPresenter to re-broadcast its latest view+cursor so a hide
      // (pointerleave) frame that was dropped during a transient outage
      // doesn't leave the laser dot stuck on every follower's screen.
      this.onPostHello()
      this.startPing()
    }

    ws.onmessage = (ev) => {
      // A frame still in flight when the consumer closed this instance must
      // not reach callbacks it has moved on from.
      if (this.closed) return
      this.lastInbound = Date.now()
      // Any frame proves the server admitted this socket. The backoff restarts
      // only once it has stayed up a while (see handleClose).
      if (!this.admittedAt) {
        this.admittedAt = Date.now()
        this.notify('open')
      }
      // The server only emits text frames; ignore Blob/ArrayBuffer payloads
      // up front rather than throwing inside JSON.parse and catching.
      if (typeof ev.data !== 'string') return
      let msg: unknown
      try {
        msg = JSON.parse(ev.data)
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
      this.handleMessage(msg as Record<string, unknown>)
    }

    ws.onclose = (ev) => this.handleClose(ev.code)

    ws.onerror = () => {
      // The browser surfaces errors before close; let onclose handle the
      // reconnect so there is only one code path.
    }
  }

  // The current socket closed (or was given up on, see dropSocket) with
  // ``code``: report it and reconnect unless the code is final.
  protected handleClose(code: number) {
    this.stopPing()
    this.ws = null
    // Once the consumer closed this instance (or an ``end`` frame did), it
    // reports nothing more. The browser still fires onclose after a
    // consumer-initiated close(), possibly carrying a server close code that
    // crossed the client's own (4000, 4404, ...); reporting that terminal state would
    // reach a consumer that has already moved on — a Stop followed by a
    // Start would tear down the new session — and a plain 'closed' would clobber
    // a fresh socket's connecting/open state or a terminal 'gone'.
    if (this.closed) return
    if (this.admittedAt && Date.now() - this.admittedAt >= STABLE_CONNECTION_MS) {
      this.reconnectIdx = 0
    }
    this.admittedAt = 0
    // 4401 is sent by sync.py's _ws_guard, the presenter hello handler and
    // the re-check loops when the credential is missing, invalid, expired
    // or revoked. Looping the reconnect with the same stale token would
    // just keep producing 4401 until the user noticed — instead, surface
    // 'unauthorized' once and stop. Callers can re-instantiate the
    // SyncPresenter / SyncFollower with a fresh token to retry.
    if (code === 4401) {
      this.closed = true
      this.notify('unauthorized')
      return
    }
    // 4403 is the IP filter refusing this client's address (at the upgrade
    // or a later re-check), or an upgrade from another site's page. Just as
    // permanent as 4401, but no sign-in fixes it, so it gets its own state.
    if (code === 4403) {
      this.closed = true
      this.notify('forbidden')
      return
    }
    // 4409 is sent by _run_presenter on an ownership mismatch: the session
    // still exists but belongs to another staff account. Retrying with the
    // same token would just keep producing 4409, so stop the loop. A room
    // that disappeared before the attach is 4404 ('gone') instead.
    if (code === 4409) {
      this.closed = true
      this.notify('notOwner')
      return
    }
    // 4000 ("presenter replaced") is sent by attach_presenter when a newer
    // presenter socket takes over this session — another tab/device of the
    // same owner, or this client's own fresh socket after a transient drop.
    // The active socket of a SyncPresenter never receives 4000 (only a stale
    // or genuinely-superseded one does), so a client that processes a 4000
    // has been replaced and must NOT reconnect: doing so would re-attach,
    // evict the replacement, get evicted again, and ping-pong forever
    // between two live tabs. Surface 'replaced' so the presenter UI can
    // release this session without ending the server-side room the other
    // socket is now driving.
    if (code === 4000) {
      this.closed = true
      this.notify('replaced')
      return
    }
    // 4400 covers "missing hello" / "name required" rejections. The
    // hello payload is rebuilt from constant state (token / name /
    // visibility) on every reconnect, so a 4400 will reproduce on every
    // attempt — stop the loop instead of hammering the server. Reported
    // as 'rejected': the user has to act (enter a name, or start again)
    // before another connection can succeed, but no login has expired.
    if (code === 4400) {
      this.closed = true
      this.notify('rejected')
      return
    }
    // 4404 means the session no longer exists on the server (rooms wiped
    // by a restart, GC reaped, or the session was ended in a way that
    // didn't deliver an ``end`` frame). Without this exit, the auto
    // reconnect loop would hammer the server with a permanently dead
    // session id every backoff tick.
    if (code === 4404) {
      this.closed = true
      this.notify('gone')
      return
    }
    // 4503 means no follower slot: the room is at FOLLOWERS_PER_ROOM_MAX or
    // this IP at its per-IP share. Surface 'full' but keep retrying with the
    // usual backoff — capacity frees up as people leave. 'open' follows once a
    // retry is seated (its first frame, as always).
    if (code === 4503) {
      this.notify('full')
      const delay = backoffWithJitter(this.reconnectIdx, this.backoffSchedule)
      this.reconnectIdx += 1
      this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.open() }, delay)
      return
    }
    // 4429 (sync.py's CLOSE_RATE_LIMITED) is deliberately absent from the
    // stop-list above. It means the per-IP login window is full right now,
    // not that the credential is bad, and the window clears on its own —
    // so falling through to the backoff reconnect below is the behaviour
    // that lets a legitimate client recover without being re-instantiated.
    // So is 1013 (CLOSE_TRY_AGAIN_LATER): the server could not check the
    // credential at all (Cloudflare Access keys or its DB unreachable), the
    // hello arrived late, this socket fell too far behind on its sends, or
    // (followers) the session's slide is unpublished for now.
    this.notify('closed')
    // Exponential backoff for transparent reconnects, jittered to avoid
    // synchronised reconnect storms across followers.
    const delay = backoffWithJitter(this.reconnectIdx, this.backoffSchedule)
    this.reconnectIdx += 1
    this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.open() }, delay)
  }

  // Give up on the current socket without waiting for its close: detach its
  // handlers, close it, and carry on as if it had closed abnormally. On a dead
  // path (a Wi-Fi roam, an expired NAT entry, a laptop waking up) a close()
  // waits for the closing handshake before onclose fires — up to a minute in
  // Chromium — on top of the ping timeout, which outlasted the server's 45 s
  // presenter grace and lost the session.
  protected dropSocket() {
    const ws = this.ws
    if (!ws) return
    ws.onopen = null
    ws.onmessage = null
    ws.onclose = null
    ws.onerror = null
    try { ws.close() } catch { /* swallow */ }
    this.handleClose(1006)
  }

  private wake() {
    if (this.closed) return
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
    if (this.reconnectTimer != null) {
      clearTimeout(this.reconnectTimer)
      // Soon rather than at once: a whole class comes back online together
      // when its access point does, and lands on one nginx handshake budget.
      this.reconnectTimer = setTimeout(
        () => { this.reconnectTimer = null; this.open() },
        backoffWithJitter(0, this.backoffSchedule),
      )
      return
    }
    if (this.ws && Date.now() - this.lastInbound > PING_TIMEOUT_MS) this.dropSocket()
  }

  protected send(msg: object): boolean {
    const ws = this.ws
    if (!ws || ws.readyState !== WebSocket.OPEN) return false
    try {
      ws.send(JSON.stringify(msg))
      return true
    } catch {
      return false
    }
  }

  protected startPing() {
    this.stopPing()
    this.pingTimer = setInterval(() => {
      if (Date.now() - this.lastInbound > PING_TIMEOUT_MS) {
        // Peer is silent: give the socket up and reconnect now (dropSocket).
        this.dropSocket()
        return
      }
      this.send({ type: 'ping' })
    }, PING_INTERVAL_MS)
  }

  protected stopPing() {
    if (this.pingTimer != null) {
      clearInterval(this.pingTimer)
      this.pingTimer = null
    }
  }

  protected notify(s: ConnState) {
    this.onStateChange?.(s)
  }

  protected abstract handleMessage(msg: Record<string, unknown>): void

  /** Override to refresh ``pendingHello`` immediately before each send. */
  protected onSocketOpen(): void { /* default: no-op */ }

  /** Override to push sticky state to the server right after the hello. */
  protected onPostHello(): void { /* default: no-op */ }

  close() {
    this.closed = true
    if (this.wakeListener) {
      window.removeEventListener('online', this.wakeListener)
      document.removeEventListener('visibilitychange', this.wakeListener)
      this.wakeListener = null
    }
    this.stopPing()
    if (this.reconnectTimer != null) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
    try { this.ws?.close() } catch { /* swallow */ }
    this.ws = null
  }
}

// ---------------------------------------------------------------- presenter

export type SyncPresenterOpts = {
  sessionId: string
  token: string
  onAudience: (a: AudienceFrame) => void
  onStateChange?: (s: ConnState) => void
  // Fired when the server warns that the session will be force-ended for
  // presenter inactivity in ``seconds``. Any view/cursor the presenter sends
  // clears it server-side; the UI can surface a "interact to keep alive" toast.
  onIdleWarning?: (seconds: number) => void
  // Fired when the server delivers an ``end`` frame, carrying its ``reason``
  // (e.g. 'presenter_idle') so the UI can explain *why* the session ended
  // rather than showing a generic notice.
  onEnded?: (reason?: string) => void
  // Fired for a server ``max_age_warning``: the session hits its absolute age
  // cap in ``seconds`` and interacting does NOT postpone it.
  onMaxAgeWarning?: (seconds: number) => void
  // Fired for a server ``notice`` frame: 'slide_unavailable' (the slide the
  // presenter moved to is not published, so followers did not follow),
  // 'slide_unpublished' (the slide being presented was unpublished meanwhile)
  // or 'unlisted_announced' (the session, announced on the index page, is now
  // on a link-only slide — see RoomManager._warn_unlisted_announced).
  onNotice?: (code: string) => void
}

export class SyncPresenter extends _BaseSync {
  protected backoffSchedule: readonly number[] = PRESENTER_RECONNECT_BACKOFF_MS
  private token: string
  private onAudience: (a: AudienceFrame) => void
  private onIdleWarning?: (seconds: number) => void
  private onEnded?: (reason?: string) => void
  private onMaxAgeWarning?: (seconds: number) => void
  private onNotice?: (code: string) => void
  // Cache the latest view + cursor that the presenter explicitly sent so
  // they can be replayed after a reconnect. send() silently drops frames
  // while the socket is reopening; without the cache, a pointerleave hide
  // frame that lands inside the outage window would never make it to the
  // server, leaving the laser dot pinned on every follower's screen.
  private latestView: Omit<ViewFrame, 'type'> | null = null
  // Starts as a hidden pointer, so a new instance's first hello is followed
  // by a hide: a Resume / takeover would otherwise leave the dot the previous tab
  // or device was showing frozen on every follower's screen until this
  // presenter happened to move over the slide.
  private latestCursor: Omit<CursorFrame, 'type'> = { x: 0, y: 0, visible: false }
  // Random id for THIS client instance, sent in every hello. A new
  // SyncPresenter (page load, Start, Resume) mints a new one; its automatic
  // reconnects reuse it with ``resume: true``, and the server refuses such a
  // reconnect (4000, 'replaced') only when a different instance has attached
  // since. The unmount cleanup passes it to endSyncSession too, so a
  // superseded tab's "I'm leaving" DELETE is ignored server-side instead of
  // tearing down the room another tab/device has taken over. Minted here
  // rather than assigned by the server on purpose: a server value reaches the
  // client only in the later ``attached`` frame, so a socket lost in between
  // would leave it stale although no other device had attached.
  readonly clientId = makeUid('p')
  // True once this instance has been seated at least once (an ``attached``
  // frame arrived). Until then every hello is a fresh attach, so a first hello
  // lost in transit can never turn into a refused "resume".
  private everAttached = false

  constructor(opts: SyncPresenterOpts) {
    super(wsUrl(`/api/sync/ws/${encodeURIComponent(opts.sessionId)}?role=presenter`))
    this.token = opts.token
    this.onAudience = opts.onAudience
    this.onStateChange = opts.onStateChange
    this.onIdleWarning = opts.onIdleWarning
    this.onEnded = opts.onEnded
    this.onMaxAgeWarning = opts.onMaxAgeWarning
    this.onNotice = opts.onNotice
    this.pendingHello = { type: 'hello', token: this.token, client_id: this.clientId }
    this.open()
  }

  protected handleMessage(msg: Record<string, unknown>): void {
    if (msg.type === 'attached') {
      this.everAttached = true
      return
    }
    if (msg.type === 'audience') {
      this.onAudience(msg as unknown as AudienceFrame)
      return
    }
    if (msg.type === 'idle_warning') {
      // Inactivity warning: the session will be force-ended in ``seconds``
      // unless the presenter interacts. Not terminal — keep the socket open.
      const secs = Number((msg as { seconds?: unknown }).seconds)
      this.onIdleWarning?.(Number.isFinite(secs) ? secs : 0)
      return
    }
    if (msg.type === 'max_age_warning') {
      const secs = Number((msg as { seconds?: unknown }).seconds)
      this.onMaxAgeWarning?.(Number.isFinite(secs) ? secs : 0)
      return
    }
    if (msg.type === 'notice') {
      const code = (msg as { code?: unknown }).code
      if (typeof code === 'string') this.onNotice?.(code)
      return
    }
    if (msg.type === 'end') {
      // Server sends ``end`` immediately before close(1000) on owner-driven
      // teardown / GC. Without this short-circuit the base class treats the
      // 1000-code close as a transient drop and re-runs the backoff loop
      // until the next attach yields a 4404 (room gone). Mirror the
      // follower's onEnd flow: stop reconnecting and surface 'gone' so the
      // hook tears local state down on the first close, not the second.
      const reason = (msg as { reason?: unknown }).reason
      this.onEnded?.(typeof reason === 'string' ? reason : undefined)
      this.closed = true
      this.notify('gone')
      try { this.ws?.close() } catch { /* swallow */ }
    }
  }

  protected onSocketOpen(): void {
    // A reconnect of an already-seated instance is marked ``resume``. If
    // another tab/device has attached since (e.g. a Resume on a tablet while
    // this laptop slept), the server refuses with 4000 ('replaced') rather than
    // letting this stale tab evict the live presenter and replay its old view.
    // The first connect (and a Resume, which builds a fresh SyncPresenter) is
    // unmarked and always takes over.
    this.pendingHello = this.everAttached
      ? { type: 'hello', token: this.token, client_id: this.clientId, resume: true }
      : { type: 'hello', token: this.token, client_id: this.clientId }
  }

  protected onPostHello(): void {
    // Replay the last view + cursor right after the hello so the server's
    // room state (and any followers that re-attached during the same drop
    // window) lands on the presenter's current intent. send() in the base
    // class no-ops if the socket isn't OPEN yet, but onPostHello fires
    // inside ws.onopen so OPEN is guaranteed here. Marked ``replay``: relayed
    // as usual, but the server does not count them as the presenter
    // interacting, so a client stuck reconnecting does not keep an abandoned
    // session alive past its inactivity timeout.
    if (this.latestView) this.send({ type: 'view', ...this.latestView, replay: true })
    this.send({ type: 'cursor', ...this.latestCursor, replay: true })
  }

  sendView(frame: Omit<ViewFrame, 'type'>): void {
    this.latestView = frame
    this.send({ type: 'view', ...frame })
  }

  sendCursor(frame: Omit<CursorFrame, 'type'>): void {
    this.latestCursor = frame
    this.send({ type: 'cursor', ...frame })
  }
}

// ----------------------------------------------------------------- follower

export type ViewportSize = { vw: number; vh: number }

export type SyncFollowerOpts = {
  sessionId: string
  name?: string | null
  onView: (f: ViewFrame) => void
  onCursor: (f: CursorFrame) => void
  onEnd: (f: EndFrame) => void
  onPresenter?: (f: PresenterFrame) => void
  // Fired for a server ``notice`` frame: 'slide_unavailable' (the slide being
  // presented was unpublished mid-session, so its tiles no longer load) and
  // 'slide_available' (it is back).
  onNotice?: (code: string) => void
  onStateChange?: (s: ConnState) => void
  // Measures the follower's viewer-canvas size for the hello frame. Called
  // immediately before each (re)connect's hello rather than once at
  // construction, so the reported size matches the socket the server is about
  // to register. Return null when the size can't be measured; the fields are
  // then omitted and the presenter panel shows the size as unknown.
  getViewport?: () => ViewportSize | null
}

function currentVisible(): boolean {
  if (typeof document === 'undefined') return true
  return document.visibilityState !== 'hidden'
}

export class SyncFollower extends _BaseSync {
  private onView: (f: ViewFrame) => void
  private onCursor: (f: CursorFrame) => void
  private onEnd: (f: EndFrame) => void
  private onPresenter?: (f: PresenterFrame) => void
  private onNotice?: (code: string) => void
  private name: string | null
  private getViewport?: () => ViewportSize | null

  constructor(opts: SyncFollowerOpts) {
    super(wsUrl(`/api/sync/ws/${encodeURIComponent(opts.sessionId)}?role=follower`))
    this.onView = opts.onView
    this.onCursor = opts.onCursor
    this.onEnd = opts.onEnd
    this.onPresenter = opts.onPresenter
    this.onNotice = opts.onNotice
    this.onStateChange = opts.onStateChange
    this.name = opts.name ?? null
    this.getViewport = opts.getViewport
    // Surface the current visibility state inside hello so a follower
    // joining with their tab in the background is registered as hidden
    // immediately, instead of being shown as "active" until they happen
    // to flip visibility once. `pendingHello` is rebuilt in onopen below
    // so a reconnect that interleaves with backgrounding/foregrounding
    // still ships an accurate value with each hello.
    this.pendingHello = this.buildHello()
    this.open()
  }

  private buildHello(): object {
    const hello: Record<string, unknown> = {
      type: 'hello',
      name: this.name,
      visible: currentVisible(),
    }
    // Omit the pair entirely when unmeasurable rather than sending nulls —
    // the server treats absent and unusable identically.
    let size: ViewportSize | null = null
    try {
      size = this.getViewport?.() ?? null
    } catch {
      size = null
    }
    if (size) {
      hello.vw = size.vw
      hello.vh = size.vh
    }
    return hello
  }

  protected onSocketOpen(): void {
    // Refresh the hello visibility right before each (re)connect's hello
    // is sent. This is also why pendingHello is mutated rather than
    // captured in the constructor — visibility state can change between
    // construction, the first connect, and any later reconnect.
    //
    // The viewport size is re-measured here for the same reason: a reconnect
    // detaches the old follower server-side and attaches a fresh one, so the
    // hello that registers it should carry the size the follower has *now*,
    // not the one it had when the socket was first constructed.
    this.pendingHello = this.buildHello()
  }

  protected handleMessage(msg: Record<string, unknown>): void {
    switch (msg.type) {
      case 'view':
        this.onView(msg as unknown as ViewFrame)
        return
      case 'cursor':
        this.onCursor(msg as unknown as CursorFrame)
        return
      case 'end':
        this.onEnd(msg as unknown as EndFrame)
        return
      case 'presenter':
        this.onPresenter?.(msg as unknown as PresenterFrame)
        return
      case 'notice': {
        const code = (msg as { code?: unknown }).code
        if (typeof code === 'string') this.onNotice?.(code)
        return
      }
    }
  }

  sendVisibility(visible: boolean): void {
    this.send({ type: 'visibility', visible })
  }
}
