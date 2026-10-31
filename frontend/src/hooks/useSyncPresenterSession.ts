/**
 * useSyncPresenterSession — owns the presenter side of a live-sync session.
 *
 * This state lives in a hook rather than inside the presenter controls so
 * ViewerPage can decide its own layout (full-bleed vs flex+sidebar) based on
 * whether a session is live. The hook owns:
 *
 *   - session / audience / connState / startError state
 *   - resumable / idleNotice state, and the probe and timers behind them
 *   - the presenter WebSocket lifecycle
 *   - the OSD view-frame sender (target frames at 5 Hz while moving, plus the
 *     settled frame)
 *   - the 15 Hz cursor sender
 *
 * Consumers receive all six state values plus the ``start`` / ``resume`` /
 * ``stop`` actions and ``dismissIdleNotice``. All teardown is handled by the
 * hook itself.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import OpenSeadragon from 'openseadragon'
import {
  createSyncSession,
  endSyncSession,
  getToken,
  listOwnedSyncSessions,
  type CreatedSyncSession,
  type SyncSessionInfo,
} from '../lib/api'
import {
  SyncPresenter,
  type AudienceFrame,
  type ConnState,
} from '../lib/sync'
import { isUnauthorizedError } from '../lib/api/shared'
import { useI18n } from '../lib/i18n'
import { apiErrorMessage } from '../lib/admin/apiError'

const EMPTY_AUDIENCE: AudienceFrame = { type: 'audience', count: 0, followers: [] }
// Minimum gap between the view frames sent while the presenter's view is
// moving (see the view-frame sender). 5 Hz keeps followers moving with the
// presenter; with the 15 Hz pointer it stays far inside the server's inbound
// budget (routers/sync.py INBOUND_FRAME_RATE).
export const TARGET_FRAME_INTERVAL_MS = 200

// The terminal refusals the presenter socket can report, and the message that
// names each cause (see the effect that handles them).
const REFUSAL_MESSAGE_KEYS: Partial<Record<ConnState, string>> = {
  unauthorized: 'sync.unauthorized',
  rejected: 'sync.presenterRejected',
  forbidden: 'sync.accessBlocked',
  notOwner: 'sync.notOwner',
}

export type SyncPresenterSession = {
  session: CreatedSyncSession | null
  audience: AudienceFrame
  connState: ConnState
  startError: string | null
  // A still-live session for this slide that the current admin previously
  // started and left (reload / tab close / network drop), or that another of
  // their tabs/devices — or a socket the server has not yet seen die — still
  // holds. Non-null only while no session is live here; surfaces the
  // launcher's "resume" affordance, which takes the session over.
  resumable: SyncSessionInfo | null
  // Inactivity notice for the presenter: a 'warning' (the server will force-end
  // in ``seconds`` unless they interact) or 'ended' (it just did). Rendered as a
  // toast by ViewerPage; cleared by presenter activity, dismissal, or a new
  // session. Null when there is nothing to show.
  idleNotice: IdleNotice | null
  dismissIdleNotice: () => void
  start: () => Promise<void>
  resume: () => Promise<void>
  stop: () => Promise<void>
}

// 'maxAgeWarning' (the absolute session-age cap is near; interacting does not
// postpone it), 'unavailable' (the slide the presenter moved to is not
// published, so followers did not follow), 'unpublished' (the slide being
// presented was unpublished meanwhile) and 'unlistedAnnounced' (the session,
// announced on the index page, moved onto a link-only slide anyone who saw the
// banner can still follow it onto) ride the same toast.
export type IdleNotice =
  | { kind: 'warning'; seconds: number }
  | { kind: 'ended'; reason?: 'presenter_idle' | 'max_age' }
  | { kind: 'maxAgeWarning'; seconds: number }
  | { kind: 'unavailable' }
  | { kind: 'unpublished' }
  | { kind: 'unlistedAnnounced' }

// The server notice codes the toast shows, and the notice each becomes.
const NOTICE_KINDS: Record<string, IdleNotice> = {
  slide_unavailable: { kind: 'unavailable' },
  slide_unpublished: { kind: 'unpublished' },
  unlisted_announced: { kind: 'unlistedAnnounced' },
}

export function useSyncPresenterSession(
  slideId: number,
  viewer: OpenSeadragon.Viewer | null,
  // Only probe for resumable sessions when the presenter UI is actually
  // eligible (admin chrome + valid staff token + published slide). The caller
  // already computes this; gating here avoids an unauthorized owned-sessions
  // request for plain public viewers.
  enabled = false,
): SyncPresenterSession {
  const [session, setSession] = useState<CreatedSyncSession | null>(null)
  const [audience, setAudience] = useState<AudienceFrame>(EMPTY_AUDIENCE)
  const [connState, setConnState] = useState<ConnState>('connecting')
  const { t } = useI18n()
  const [startError, setStartError] = useState<string | null>(null)
  const [resumable, setResumable] = useState<SyncSessionInfo | null>(null)
  const [idleNotice, setIdleNotice] = useState<IdleNotice | null>(null)
  // Drop a pending inactivity *warning* (not an 'ended' notice). Called
  // whenever the presenter sends a frame, mirroring the server-side reset so
  // the toast disappears the moment they interact. ``setIdleNotice`` is a
  // stable setter, so callers don't need it in their effect deps.
  const clearIdleWarning = () => {
    setIdleNotice((n) => (n && n.kind === 'warning' ? null : n))
  }
  // Keep the latest clearer in a ref so the long-lived sender effects (which
  // intentionally don't re-run on every render) always call the current one
  // without listing it as a dependency.
  const clearIdleWarningRef = useRef(clearIdleWarning)
  clearIdleWarningRef.current = clearIdleWarning
  // Drop every notice about the live session (a countdown, a warning) once
  // there is no session here any more — gone, taken over, refused — keeping
  // only an 'ended' one, which explains why. A countdown left up would tick
  // to 0 over a session that is not ending, or already ended elsewhere, and
  // the idle warning cannot be dismissed.
  const dropLiveNotice = () => {
    setIdleNotice((n) => (n && n.kind === 'ended' ? n : null))
  }
  // True once an ``end`` frame named its reason in a toast (inactivity, the
  // age cap), so the 'gone' that follows does not also pin a generic "session
  // ended" under the launcher until the next Start.
  const endExplainedRef = useRef(false)
  // The session the presenter stopped here. The resume probe that runs once
  // it is cleared can be answered before the DELETE lands (both are a staff
  // check on the server's threadpool), and offered to resume a dead session.
  const stoppedSidRef = useRef<string | null>(null)
  const presenterRef = useRef<SyncPresenter | null>(null)
  // Identifies the SyncPresenter the hook's state currently belongs to, like
  // useSyncFollower's connGenRef. Bumped whenever presenterRef is replaced or
  // released; each instance's callbacks capture the value they were built
  // under and drop everything once it no longer matches. Without the fence a
  // superseded instance's late report — the ``end`` frame of the room a Stop
  // just ended, say — would land on the shared state after a Start had already
  // seated the next session, and the 'gone' handling below would close that new
  // socket and forget its room.
  const presenterGenRef = useRef(0)
  // Socket callbacks for one new SyncPresenter (start() and resume()); claims
  // a fresh generation, so call it right before constructing the instance.
  // Only stable setters and refs inside.
  const presenterCallbacks = useCallback(() => {
    const gen = ++presenterGenRef.current
    const live = () => presenterGenRef.current === gen
    return {
      onAudience: (a: AudienceFrame) => { if (live()) setAudience(a) },
      onStateChange: (s: ConnState) => { if (live()) setConnState(s) },
      onIdleWarning: (seconds: number) => {
        if (live()) setIdleNotice({ kind: 'warning', seconds })
      },
      onMaxAgeWarning: (seconds: number) => {
        if (live()) setIdleNotice({ kind: 'maxAgeWarning', seconds })
      },
      onNotice: (code: string) => {
        const notice = NOTICE_KINDS[code]
        // Never over a countdown: these notices can come from the server's
        // periodic slide re-check, not from anything the presenter did, and
        // replacing the idle countdown (which cannot be dismissed, and is
        // the only warning before the session ends) or the age-cap warning
        // with a toast that auto-dismisses would leave the presenter unwarned.
        if (live() && notice) {
          setIdleNotice((n) => (n && (n.kind === 'warning' || n.kind === 'maxAgeWarning') ? n : { ...notice }))
        }
      },
      onEnded: (reason?: string) => {
        if (live() && (reason === 'presenter_idle' || reason === 'max_age')) {
          endExplainedRef.current = true
          setIdleNotice({ kind: 'ended', reason })
        }
      },
    }
  }, [])
  // Close the current SyncPresenter (if any) and fence off its callbacks.
  // Returns the released instance so the unmount cleanup can still read its
  // client id.
  const releasePresenter = useCallback((): SyncPresenter | null => {
    presenterGenRef.current += 1
    const p = presenterRef.current
    presenterRef.current = null
    try { p?.close() } catch { /* swallow */ }
    return p
  }, [])
  // A failed start: no token at all reads as "log in", a rejected one as
  // "session expired" (apiErrorMessage), a coded server refusal in its own
  // words.
  const startErrorMessage = useCallback((e: unknown): string => {
    if (isUnauthorizedError(e) && !getToken()) return t('sync.loginRequired') as string
    return apiErrorMessage(e, t) || (t('sync.startError') as string)
  }, [t])
  const sessionId = session?.session_id ?? null
  // Keep a ref to the latest session ID so the unmount cleanup can end the
  // server session even when the presenter navigates away without clicking End.
  const sessionIdRef = useRef<string | null>(null)
  // Concurrency guard for start(). A second call while the first is still
  // awaiting createSyncSession would otherwise create a duplicate server-side
  // room and leak the first SyncPresenter (presenterRef gets overwritten by
  // the second call without close()). Dropping the second click is the right
  // UX: the first call is already producing the requested session.
  const startingRef = useRef(false)
  // Cancellation handle for an in-flight start(). Unmount cleanup flips
  // ``cancelled`` so the resume path after ``await createSyncSession`` can
  // tear the freshly-created server room down instead of plumbing it into
  // a dead component. Without this, an unmount that lands inside the
  // ``await createSyncSession`` window leaves no sid on the ref, the
  // unmount cleanup's DELETE is skipped, and the resume builds a stranded
  // SyncPresenter that keeps trying to attach against an orphaned room
  // until the presenter-grace GC reaps it 45 s later.
  const startCancelRef = useRef<{ cancelled: boolean } | null>(null)
  useEffect(() => {
    sessionIdRef.current = sessionId
  }, [sessionId])

  // ---------- start / stop -------------------------------------------------
  const start = useCallback(async () => {
    if (startingRef.current) return
    startingRef.current = true
    const cancel = { cancelled: false }
    startCancelRef.current = cancel
    try {
      // Close any pre-existing presenter from a stale start before the new
      // round trip so a WebSocket doesn't leak if the previous session was
      // somehow left attached.
      releasePresenter()
      setStartError(null)
      setIdleNotice(null)
      endExplainedRef.current = false
      // Starting supersedes every live session of this account, wherever it
      // runs: one on another slide, or one another tab / device took over
      // since the resume probe ran, ended for all its followers with no
      // warning. Look again now, and ask before replacing one.
      let owned: SyncSessionInfo[] = []
      try {
        owned = (await listOwnedSyncSessions())
          .filter((s) => s.session_id !== stoppedSidRef.current)
      } catch { /* the create below still works; nothing to warn about */ }
      if (cancel.cancelled) return
      if (owned.length > 0) {
        const live = owned.some((s) => s.presenter_connected)
        if (!window.confirm(t(live ? 'sync.confirmSupersedeLive' : 'sync.confirmSupersede') as string)) {
          // Offer the one on this slide for resuming instead, if any.
          setResumable(owned.find((s) => s.slide_id === slideId) ?? null)
          return
        }
      }
      let created: CreatedSyncSession | null = null
      try {
        created = await createSyncSession(slideId)
        // Component unmounted while createSyncSession was in flight. The
        // unmount cleanup ran with sessionIdRef.current still null and
        // therefore couldn't issue a DELETE — end the room here so it
        // doesn't sit orphaned until the presenter-grace GC reaps it.
        // Skip the SyncPresenter creation entirely; the component is gone.
        if (cancel.cancelled) {
          try { await endSyncSession(created.session_id) } catch { /* swallow */ }
          return
        }
        // Pin the just-created session id to the ref *before* anything else
        // can throw or short-circuit. The unmount-cleanup effect reads this
        // ref to decide whether to issue endSyncSession on tear-down.
        sessionIdRef.current = created.session_id
        const token = getToken()
        if (!token) {
          // Token disappeared between createSyncSession's request and its
          // response (the admin logged out). The room already exists on the
          // server, but without a token no DELETE can be sent (endSyncSession
          // refuses at once); no presenter ever attaches to it, so the grace
          // GC reaps it within WSI_SYNC_PRESENTER_GRACE_SEC.
          setStartError(t('sync.loginRequired') as string)
          try { await endSyncSession(created.session_id) } catch { /* swallow */ }
          sessionIdRef.current = null
          return
        }
        setSession(created)
        // A fresh session supersedes any resumable one being offered.
        setResumable(null)
        // Attach the presenter WebSocket immediately so the audience panel
        // receives its initial (empty) snapshot.
        presenterRef.current = new SyncPresenter({
          sessionId: created.session_id,
          token,
          ...presenterCallbacks(),
        })
      } catch (e) {
        // If createSyncSession itself failed, `created` is still null and
        // there is nothing to clean up. If a later step threw after the
        // server already created the room, end it to avoid leaking.
        if (created) {
          try { await endSyncSession(created.session_id) } catch { /* swallow */ }
          sessionIdRef.current = null
          // A throw after setSession(created) (e.g. the WebSocket constructor
          // raising SecurityError on a blocked/invalid URL) would otherwise
          // leave `session` non-null with no socket and no server room behind
          // it — the sidebar would render a dead "live" session recoverable
          // only by reload. Clear it back out like every other teardown path.
          setSession(null)
          setAudience(EMPTY_AUDIENCE)
        }
        setStartError(startErrorMessage(e))
      }
    } finally {
      startingRef.current = false
      if (startCancelRef.current === cancel) startCancelRef.current = null
    }
  }, [slideId, t, presenterCallbacks, releasePresenter, startErrorMessage])

  // ---------- resume -------------------------------------------------------
  // Reattach to a still-live session this admin previously left, without
  // minting a new room (which would orphan the old one and double-notify the
  // discovery audience). The session id comes from the owned-sessions probe
  // below; the server enforces the owner check on the presenter handshake.
  const resume = useCallback(async () => {
    if (startingRef.current) return
    const target = resumable
    if (!target) return
    startingRef.current = true
    try {
      releasePresenter()
      setStartError(null)
      setIdleNotice(null)
      endExplainedRef.current = false
      const token = getToken()
      if (!token) {
        setStartError(t('sync.loginRequired') as string)
        return
      }
      // Prefer the server-built follower_url from the owned-sessions list: it
      // is canonicalised the same way create() does (honouring
      // WSI_PUBLIC_BASE_URL), so a presenter reachable under multiple
      // names/addresses still hands out the operator's canonical share link on
      // resume. Fall back to reconstructing it from the live origin only if the
      // server didn't supply one (no slide public_id to build it from), which
      // is exactly right for a single-address deployment anyway.
      const created: CreatedSyncSession = {
        session_id: target.session_id,
        slide_id: target.slide_id,
        slide_public_id: target.slide_public_id,
        // Viewer URLs are keyed by the slide's public_id token, so the
        // fallback share link is built from it (never the numeric id).
        follower_url:
          target.follower_url
          || `${window.location.origin}/slides/${encodeURIComponent(target.slide_public_id)}?sync=${encodeURIComponent(target.session_id)}`,
      }
      // Pin the id before attaching so the unmount cleanup can end the room if
      // the presenter navigates away again. If the room was already reaped
      // (grace elapsed between probe and click), the WS attach fails 4404 and
      // the 'gone' effect below clears this back out.
      sessionIdRef.current = created.session_id
      setSession(created)
      setResumable(null)
      try {
        presenterRef.current = new SyncPresenter({
          sessionId: created.session_id,
          token,
          ...presenterCallbacks(),
        })
      } catch (e) {
        // The WebSocket constructor can throw (a SecurityError on a blocked
        // URL), as start() guards against too: without this the hook would keep a
        // "live" session with no socket behind it. The room is the presenter's
        // to resume again, so it is not ended — and Resume is offered for it
        // again (the probe does not re-run: the session never left null).
        sessionIdRef.current = null
        setSession(null)
        setAudience(EMPTY_AUDIENCE)
        setResumable(target)
        setStartError(startErrorMessage(e))
      }
    } finally {
      startingRef.current = false
    }
  }, [resumable, t, presenterCallbacks, releasePresenter, startErrorMessage])

  // ---------- resumable-session probe --------------------------------------
  // On mount (and whenever the slide / eligibility changes), ask the server
  // whether this admin owns a still-live session for the current slide. If so,
  // surface it so the launcher can offer "resume" instead of "start". That
  // includes a session the server still sees a presenter attached to (its
  // ``presenter_connected``): after a network drop or a crashed tab the server
  // holds the dead socket until its keepalive notices, and Start would then
  // supersede the session and end it for every follower, whereas Resume takes
  // it over. Skipped once a session is live here, and disabled for
  // non-presenter viewers.
  useEffect(() => {
    if (!enabled || session) {
      setResumable(null)
      return
    }
    let cancelled = false
    ;(async () => {
      try {
        const owned = await listOwnedSyncSessions()
        if (cancelled) return
        setResumable(
          owned.find((s) => s.slide_id === slideId && s.session_id !== stoppedSidRef.current) ?? null,
        )
      } catch {
        if (!cancelled) setResumable(null)
      }
    })()
    return () => { cancelled = true }
  }, [enabled, slideId, session])

  const stop = useCallback(async () => {
    const sid = sessionId
    stoppedSidRef.current = sid
    releasePresenter()
    // Clear the ref synchronously so an unmount that races the await below
    // doesn't fire a second DELETE from the unmount cleanup. The server
    // 404s on the duplicate either way, but skipping the redundant request
    // avoids spurious access-log noise during a fast stop→navigate flow.
    sessionIdRef.current = null
    setSession(null)
    setAudience(EMPTY_AUDIENCE)
    // Operator ended it deliberately — no inactivity toast should linger.
    setIdleNotice(null)
    if (sid) {
      try { await endSyncSession(sid) } catch { /* swallow — server may already be tearing down */ }
    }
  }, [sessionId, releasePresenter])

  // 'gone' indicates the server returned 4404 — the room no longer exists
  // (server restart wiped in-memory state, or a teardown path didn't
  // deliver the ``end`` frame in time). Clear local session state so the
  // launcher button reappears instead of leaving the sidebar pinned to a
  // dead session id.
  useEffect(() => {
    if (connState !== 'gone') return
    releasePresenter()
    sessionIdRef.current = null
    setSession(null)
    setAudience(EMPTY_AUDIENCE)
    dropLiveNotice()
    // An end explained by its toast needs no second, sticky message.
    if (!endExplainedRef.current) setStartError(t('sync.sessionEnded') as string)
  }, [connState])

  // 'replaced' indicates a newer presenter socket took over this session
  // (the admin opened a second tab/device and resumed, or this client raced
  // its own reconnect). The WebSocket has already self-closed and stopped
  // reconnecting to avoid a ping-pong eviction loop. Release local session
  // state so this tab returns to the launcher — but do NOT endSyncSession:
  // the room is still live and being driven by the socket that replaced this one,
  // and sessionIdRef is cleared so the unmount cleanup won't DELETE it either.
  useEffect(() => {
    if (connState !== 'replaced') return
    releasePresenter()
    sessionIdRef.current = null
    setSession(null)
    setAudience(EMPTY_AUDIENCE)
    dropLiveNotice()
    setStartError(t('sync.replaced') as string)
  }, [connState])

  // The server refused this presenter for good, each with its own message:
  //   'unauthorized' — 4401: the staff credential is invalid, expired or was
  //     revoked mid-session (reauth loop: account disabled / demoted); a fresh
  //     sign-in is the fix.
  //   'rejected' — 4400: the hello was malformed (a late one gets the
  //     retryable 1013 instead); starting again is the fix.
  //   'forbidden' — 4403: the IP filter denies this address (or the page's
  //     origin was refused); signing in again would not help.
  //   'notOwner' — 4409: the session belongs to another staff account.
  // The WebSocket has already self-closed; clear session state so the
  // launcher button reappears. Without this, the sidebar stays pinned to a
  // dead session with no recovery path except a full page reload.
  useEffect(() => {
    const key = REFUSAL_MESSAGE_KEYS[connState]
    if (!key) return
    releasePresenter()
    sessionIdRef.current = null
    setSession(null)
    setAudience(EMPTY_AUDIENCE)
    dropLiveNotice()
    setStartError(t(key) as string)
  }, [connState])

  // Tear the WS down on unmount and end the server session so navigating away
  // without clicking End does not leave a stale session (and banner) behind.
  // If stop() already called endSyncSession, the server returns 404 which is
  // silently ignored by endSyncSession — no double-end harm.
  useEffect(() => {
    return () => {
      // Signal any in-flight start() to abort. The resume path after
      // ``await createSyncSession`` will end the freshly-created room
      // and skip building a SyncPresenter for the now-unmounted component.
      if (startCancelRef.current) startCancelRef.current.cancelled = true
      const sid = sessionIdRef.current
      // Scope the "end because I'm navigating away" DELETE to this tab's
      // presenter client id: if another tab/device has attached since, the
      // server ignores this teardown so it can't kill the room that one is
      // now driving. The id is minted client-side, so — unlike a value the
      // server hands back — it is never stale, even when the socket died
      // before its attach was confirmed.
      const released = releasePresenter()
      if (sid) {
        endSyncSession(sid, released?.clientId).catch(() => { /* swallow */ })
      }
    }
  }, [releasePresenter])

  // ---------- view-frame sender ---------------------------------------------
  // Two kinds of frame. While the view moves, the rect OSD's springs are
  // heading for (the *target*, getBounds(false)) goes out at most every
  // TARGET_FRAME_INTERVAL_MS, so followers start the same zoom / pan at once
  // and animate alongside the presenter. Sending only once motion settled would make
  // them start a whole spring animation late (≈1.2 s), then run their own —
  // over two seconds behind a wheel zoom, while the 15 Hz pointer was already
  // pointing at a region they could not see yet. When motion settles the
  // exact current rect goes out once more as the authoritative frame. A frame
  // identical to the last one sent is skipped, so a spring running toward an
  // unchanged target sends nothing more.
  useEffect(() => {
    if (!viewer || !presenterRef.current || !sessionId) return
    let settleTimer: ReturnType<typeof setTimeout> | null = null
    let throttleTimer: ReturnType<typeof setTimeout> | null = null
    let lastSentMs = 0
    // The last frame sent, to skip an identical one (compared field by field:
    // a running spring asks on every animation step, ~60 times a second).
    let lastSent: { bounds: number[]; bounds_deg: number; rotation_deg: number } | null = null

    const send = (current: boolean) => {
      const p = presenterRef.current
      // Nothing to send until the image is open: ViewerPage publishes the
      // viewer as soon as it is constructed, before the saved viewport is
      // restored, and the default home view it shows meanwhile would snap
      // every follower to it (and be cached for the reconnect replay).
      if (!p || !viewer.isOpen()) return
      try {
        const b = viewer.viewport.getBounds(current)
        // OSD's rotateTo() does not wrap (five quarter-turns report 450°),
        // so fold into [0, 360) before sending. The server normalises too.
        const rawRotation = viewer.viewport.getRotation(current) || 0
        const rotation = ((rawRotation % 360) + 360) % 360
        const frame = {
          slide_id: slideId,
          // Carry the rect's own degrees so the follower can reconstruct the
          // exact Rect getBounds() produced. Dropping it (rebuilding with
          // degrees 0) desyncs every follower the moment the presenter
          // rotates, because b.x/b.y are the rotated corner, not the
          // axis-aligned top-left.
          bounds: [b.x, b.y, b.width, b.height] as [number, number, number, number],
          bounds_deg: b.degrees || 0,
          rotation_deg: rotation,
        }
        if (
          lastSent
          && lastSent.bounds_deg === frame.bounds_deg
          && lastSent.rotation_deg === frame.rotation_deg
          && lastSent.bounds.every((n, i) => n === frame.bounds[i])
        ) return
        p.sendView(frame)
        lastSent = frame
        lastSentMs = Date.now()
        // Presenter is interacting — drop any inactivity warning toast.
        clearIdleWarningRef.current()
      } catch { /* swallow */ }
    }

    // Leading edge at once, then at most one target frame per interval, the
    // trailing one reading the target as it is by then.
    const onViewportChange = () => {
      if (throttleTimer != null) return
      const wait = TARGET_FRAME_INTERVAL_MS - (Date.now() - lastSentMs)
      if (wait <= 0) {
        send(false)
      } else {
        throttleTimer = setTimeout(() => { throttleTimer = null; send(false) }, wait)
      }
    }
    const onAnimationFinish = () => {
      if (settleTimer != null) clearTimeout(settleTimer)
      settleTimer = setTimeout(() => { settleTimer = null; send(true) }, 150)
    }
    // A new animation inside those 150 ms (the next wheel notch) makes the
    // settled frame stale: sent anyway, it would pull every follower back to a
    // point partway along the new zoom before the next target frame sent them
    // on. The new animation's own finish sends it instead.
    const onAnimationStart = () => {
      if (settleTimer != null) { clearTimeout(settleTimer); settleTimer = null }
    }

    viewer.addHandler('viewport-change', onViewportChange)
    viewer.addHandler('animation-finish', onAnimationFinish)
    viewer.addHandler('animation-start', onAnimationStart)

    // Send once on mount so a freshly attached follower lands on the right
    // view immediately even if the presenter never moves — but only once the
    // image is open (see send). Registering after ViewerPage's own once-`open`
    // handler means this runs after the saved viewport is restored there.
    const onOpen = () => send(true)
    if (viewer.isOpen()) {
      send(true)
    } else {
      viewer.addOnceHandler('open', onOpen)
    }

    return () => {
      if (settleTimer != null) clearTimeout(settleTimer)
      if (throttleTimer != null) clearTimeout(throttleTimer)
      try { viewer.removeHandler('open', onOpen) } catch { /* swallow */ }
      try { viewer.removeHandler('viewport-change', onViewportChange) } catch { /* swallow */ }
      try { viewer.removeHandler('animation-finish', onAnimationFinish) } catch { /* swallow */ }
      try { viewer.removeHandler('animation-start', onAnimationStart) } catch { /* swallow */ }
    }
  }, [viewer, sessionId, slideId])

  // ---------- cursor sender (15 Hz throttle) -------------------------------
  useEffect(() => {
    if (!viewer || !presenterRef.current || !sessionId) return
    const el = viewer.element
    if (!el) return
    let lastSentMs = 0
    let pending: { x: number; y: number; visible: boolean } | null = null
    let timer: ReturnType<typeof setTimeout> | null = null
    // Whether the last pointer frame sent was a visible one, i.e. whether
    // followers are being shown a dot this effect has to take down.
    let shown = false
    // 15 Hz: a laser pointer over a static slide reads as smooth at this rate
    // once the follower CSS-interpolates between frames (see reprojectCursor),
    // and it halves the steady-state fan-out vs a 30 Hz rate — meaningful at
    // 50-100 followers where the presenter is the sole sender to all of them.
    const minInterval = 1000 / 15
    // Cumulative micro-move filter: drop a move whose element-pixel position is
    // within MOVE_EPSILON_PX of the last one queued. Compared against the
    // last *queued* position (not the last event) so slow drift still
    // accumulates past the threshold and is sent — it only swallows hand
    // jitter while the pointer is effectively stationary, sparing followers a
    // stream of identical frames.
    const MOVE_EPSILON_PX = 2
    let lastQueuedPx: number | null = null
    let lastQueuedPy: number | null = null
    // The image point the last queued frame carried, and where the pointer is
    // now (element pixels; null while it is off the slide) — to notice the
    // view moving under a pointer that does not.
    let lastQueuedImg: OpenSeadragon.Point | null = null
    let pointer: { x: number; y: number } | null = null

    // Element pixel <-> image pixel, against the viewport's target (as OSD
    // maps its own pointer input). The view is never flipped (the ``f`` key
    // is off, see attachNoFlipKey), so no mirroring is needed.
    const imageAt = (px: number, py: number) => viewer.viewport.viewportToImageCoordinates(
      viewer.viewport.pointFromPixel(new OpenSeadragon.Point(px, py)),
    )
    const pixelOf = (img: OpenSeadragon.Point) =>
      viewer.viewport.pixelFromPoint(viewer.viewport.imageToViewportCoordinates(img), false)

    const flush = () => {
      timer = null
      if (!pending) return
      presenterRef.current?.sendCursor(pending)
      shown = pending.visible
      lastSentMs = Date.now()
      pending = null
      // Cursor motion counts as interaction — clear any inactivity warning.
      clearIdleWarningRef.current()
    }

    const enqueue = (msg: { x: number; y: number; visible: boolean }) => {
      pending = msg
      const now = Date.now()
      const wait = Math.max(0, minInterval - (now - lastSentMs))
      if (wait === 0) {
        flush()
      } else if (!timer) {
        timer = setTimeout(flush, wait)
      }
    }

    const queueAt = (px: number, py: number) => {
      try {
        const img = imageAt(px, py)
        lastQueuedPx = px
        lastQueuedPy = py
        lastQueuedImg = img
        enqueue({ x: img.x, y: img.y, visible: true })
      } catch { /* swallow */ }
    }

    const onMove = (ev: PointerEvent) => {
      // Only the primary pointer: in a two-finger pinch on a tablet both
      // fingers report moves, and following both made the dot jump between
      // them at the frame rate.
      if (ev.isPrimary === false) return
      // Nothing to point at until the image is open. A slide switch builds
      // the next viewer before its image loads, and OSD then maps the pointer
      // onto a 1×1 placeholder: followers, still on the previous slide until
      // the new view frame arrives, would see the dot at its top-left corner.
      if (!viewer.viewport || !viewer.isOpen()) return
      const rect = el.getBoundingClientRect()
      const px = ev.clientX - rect.left
      const py = ev.clientY - rect.top
      pointer = { x: px, y: py }
      if (
        lastQueuedPx !== null
        && lastQueuedPy !== null
        && Math.abs(px - lastQueuedPx) < MOVE_EPSILON_PX
        && Math.abs(py - lastQueuedPy) < MOVE_EPSILON_PX
      ) {
        return
      }
      queueAt(px, py)
    }

    // The view can move under a pointer that does not: keyboard panning, the
    // zoom / home buttons, a flick's inertia, a constraint snapping back.
    // Re-aim the dot once the image point last sent no longer lies under the
    // pointer. Measured against the target, so a running spring costs one
    // frame per new target rather than one per animation step, and a wheel
    // zoom (which keeps the point under the pointer) or a drag costs none.
    const onViewportChange = () => {
      if (!pointer || !lastQueuedImg || !viewer.isOpen()) return
      try {
        const p = pixelOf(lastQueuedImg)
        if (
          Math.abs(p.x - pointer.x) < MOVE_EPSILON_PX
          && Math.abs(p.y - pointer.y) < MOVE_EPSILON_PX
        ) {
          return
        }
        queueAt(pointer.x, pointer.y)
      } catch { /* swallow */ }
    }

    // Take the dot down at once, bypassing the throttle.
    const hide = () => {
      if (timer != null) { clearTimeout(timer); timer = null }
      pending = null
      pointer = null
      // Reset the dedup anchor so the first move after re-entry is always sent,
      // even if it lands within MOVE_EPSILON_PX of wherever the pointer left.
      lastQueuedPx = null
      lastQueuedPy = null
      lastQueuedImg = null
      if (!shown) return
      presenterRef.current?.sendCursor({ x: 0, y: 0, visible: false })
      shown = false
      lastSentMs = Date.now()
    }
    const onLeave = (ev: PointerEvent) => {
      // A second finger lifting fires its own pointerleave; the primary one
      // is still on the slide.
      if (ev.isPrimary === false) return
      hide()
    }
    // Switching to another window or app (Alt-Tab, a click outside the
    // browser) or hiding the tab moves no pointer out of the slide, so no
    // pointerleave fires and the dot would stay frozen on every follower.
    const onWindowBlur = () => hide()
    const onVisibility = () => { if (document.visibilityState === 'hidden') hide() }

    el.addEventListener('pointermove', onMove)
    el.addEventListener('pointerleave', onLeave)
    // pointercancel fires when a touch/pen stroke is aborted by the system
    // (iOS edge-swipe, context menu, palm rejection) without a matching
    // pointerleave. Without hiding on it, the follower's laser dot freezes
    // at the last position and the cached visible:true frame resurrects it
    // on the next reconnect replay.
    el.addEventListener('pointercancel', onLeave)
    window.addEventListener('blur', onWindowBlur)
    document.addEventListener('visibilitychange', onVisibility)
    viewer.addHandler('viewport-change', onViewportChange)
    return () => {
      if (timer != null) clearTimeout(timer)
      el.removeEventListener('pointermove', onMove)
      el.removeEventListener('pointerleave', onLeave)
      el.removeEventListener('pointercancel', onLeave)
      window.removeEventListener('blur', onWindowBlur)
      document.removeEventListener('visibilitychange', onVisibility)
      try { viewer.removeHandler('viewport-change', onViewportChange) } catch { /* swallow */ }
      // The dot's image coordinates belong to this viewer's slide. Take it
      // down when the viewer goes (a slide switch), or it would be shown on
      // the next slide — by the reconnect replay, if nothing else. A Stop has
      // already released the presenter, so nothing is sent then.
      if (shown) presenterRef.current?.sendCursor({ x: 0, y: 0, visible: false })
    }
  }, [viewer, sessionId])

  const dismissIdleNotice = useCallback(() => setIdleNotice(null), [])

  return {
    session, audience, connState, startError, resumable,
    idleNotice, dismissIdleNotice,
    start, resume, stop,
  }
}
