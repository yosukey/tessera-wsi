/**
 * useSyncFollower — owns the follower side of a live-sync session.
 *
 * Isolates the peek→join→connected/ended/notfound state machine so it
 * can be understood and tested on its own. The hook owns:
 *
 *   - followerStatus / followerSlideRef / followerConnState /
 *     followerHasConnected state
 *   - the peek fetch that gates the join dialog
 *   - the SyncFollower WebSocket lifecycle
 *   - remote view-frame application (applyViewFrame / handleRemoteView)
 *   - cursor reprojection (handleRemoteCursor / reprojectCursor)
 *   - tab-visibility reporting
 *
 * Consumers receive state values, the cursor overlay ref (attach to a DOM
 * node), a refitToLatestView callback (call when the OSD
 * image opens and after the viewer resizes), a reprojectCursor callback (call
 * from the OSD update-viewport handler), and the joinSession / cancelJoin /
 * leaveSession actions.
 *
 * One socket per session: the SyncFollower is opened once on Join and kept
 * for the whole session, surviving presenter-driven slide changes. ViewerPage
 * is not remounted when the URL slide id changes (the route has no key), so a
 * slide change only retargets the OSD viewer; the socket stays attached and
 * handleRemoteView reads the current slide via slidePublicIdRef. The socket is torn
 * down only on leave / end / terminal connState / a change or removal of the
 * ``?sync=`` session id / unmount.
 */
import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import OpenSeadragon from 'openseadragon'
import { peekSyncSession } from '../lib/api'
import { isSyncPeekUnavailableError } from '../lib/api/sync'
import { logger } from '../lib/logger'
import { retryDelayMs } from '../lib/retryDelay'
import { SyncFollower, type ConnState, type CursorFrame, type EndFrame, type ViewFrame } from '../lib/sync'

// How many times a peek the server could not answer right now (429, 5xx, no
// response) is retried before the follower is shown the error, with its retry
// button: with retryDelayMs that is about a minute.
const PEEK_RETRIES = 7

export type FollowerStatus =
  | 'idle' | 'peek' | 'join' | 'connected' | 'ended' | 'notfound' | 'error' | 'full' | 'unauthorized' | 'forbidden'

export function useSyncFollower(
  syncId: string,
  slidePublicId: string,
  osdRef: { current: OpenSeadragon.Viewer | null },
) {
  const nav = useNavigate()
  const [searchParams] = useSearchParams()
  // Pinning searchParams in a ref lets the async peek/onView handlers read
  // the *current* query string without forcing the effects below to re-fire
  // every time the URL changes — a re-fire on the peek effect would re-issue
  // the API call and bounce the join dialog back to the peek state.
  const searchParamsRef = useRef(searchParams)
  useEffect(() => {
    searchParamsRef.current = searchParams
  }, [searchParams])
  const isFollower = syncId.length > 0

  const [followerStatus, setFollowerStatus] = useState<FollowerStatus>(
    isFollower ? 'peek' : 'idle',
  )
  const [followerSlideRef, setFollowerSlideRef] = useState<{
    catalog_id: string | null
    title: string | null
  } | null>(null)
  // Tracks the server's WSI_SYNC_REQUIRE_NAME from the peek response so the
  // Join dialog can hide its "Join anonymously" button. Without it, an
  // anonymous follower would survive the dialog only to be refused by the
  // server with a 4400 close, which sync.ts treats as final ('rejected').
  const [followerRequireName, setFollowerRequireName] = useState(false)
  const [followerConnState, setFollowerConnState] = useState<ConnState>('connecting')
  const [followerHasConnected, setFollowerHasConnected] = useState(false)
  // Reason carried by the server's ``end`` frame (e.g. 'presenter_idle'), so
  // the ended overlay can explain *why* instead of a generic "session ended".
  const [endedReason, setEndedReason] = useState<string | null>(null)
  // The session a ``superseded`` end names as its replacement, if any: the
  // ended overlay offers to join it.
  const [endedNext, setEndedNext] = useState<EndFrame['next'] | null>(null)
  // True while the server says the slide being presented was unpublished
  // mid-session (its tiles no longer load); cleared when it is back or the
  // presenter moves to another slide.
  const [followerSlideUnavailable, setFollowerSlideUnavailable] = useState(false)
  // Bumped by retryPeek to run the session effect (and its peek) again.
  const [peekRun, setPeekRun] = useState(0)
  // Presenter-absence state, driven by the server's `presenter` frames. When
  // the presenter drops mid-session the room lingers for a grace window; the
  // follower shows a "presenter left — ending in Ns" countdown that clears if
  // the presenter returns (present: true) and otherwise runs out as the server
  // tears the session down (delivering the usual `end` frame).
  const [presenterAbsent, setPresenterAbsent] = useState(false)
  const [presenterGraceRemaining, setPresenterGraceRemaining] = useState<number | null>(null)
  const graceTimerRef = useRef<number | null>(null)

  const followerSocketRef = useRef<SyncFollower | null>(null)
  // Identifies the socket the hook's state currently belongs to. Bumped on
  // every join and on every session teardown; each SyncFollower captures the
  // value it was built under and its callbacks drop anything once it no longer
  // matches. sync.ts already goes quiet once close() is called (no state
  // report, no frame), so this is the hook's own guarantee on top: whatever a
  // superseded socket still delivers — a terminal close code that would flip
  // the live session to 'ended' and make the 'gone' effect below close the
  // *live* socket, or a frame from a session the URL has left, which would
  // navigate the page and pair the old session's slide with the new
  // ``?sync=`` query — never reaches the current session's state.
  const connGenRef = useRef(0)
  const latestViewRef = useRef<ViewFrame | null>(null)
  const animFinishCleanupRef = useRef<(() => void) | null>(null)
  const cursorOverlayRef = useRef<HTMLDivElement | null>(null)
  const lastCursorRef = useRef<CursorFrame | null>(null)
  // handleRemoteView is captured ONCE inside the SyncFollower closure (the
  // socket calls that construction-time function for the whole session), so
  // it must compare incoming frames against the CURRENT slide token — not the
  // value that was in scope when joinSession() built the socket. Mirror the
  // prop into a ref, updated synchronously below, and read
  // slidePublicIdRef.current from the closure. This is what lets one socket
  // survive presenter-driven slide changes instead of being torn down and
  // rebuilt with a fresh closure. Frames/peeks carry the slide's public_id
  // (the numeric slide_id stays server-internal), so matching + navigation
  // are both by token.
  const slidePublicIdRef = useRef(slidePublicId)
  useEffect(() => { slidePublicIdRef.current = slidePublicId }, [slidePublicId])

  // ---- Follower lifecycle (peek -> join dialog -> WS) --------------------
  // Peek the session before the join dialog to show the catalog id +
  // title. peekSyncSession() returns null for a real 404 (session ended). A
  // peek the server could not answer right now (429, 5xx, no response) is
  // retried, PEEK_RETRIES times over about a minute; that, or any other
  // failure, ends as a distinct 'error' status (with a Retry button) so the UI
  // can tell "no such session" apart from "the server was unreachable".
  //
  // Keyed on the session (``[syncId, isFollower]``) — NOT on slidePublicId — so it
  // runs once per session. ViewerPage is not remounted when the slide id in
  // the URL changes (the route has no ``key``), and the follower socket lives
  // for the whole session, so a slide change must not re-run peek/join. The
  // wrong-slide-on-open redirect below navigates in place (no remount), so it
  // doesn't need a router-state hand-off to avoid a second peek.
  //
  // The same no-remount property means the *session* can change under a
  // mounted hook too: editing ``?sync=A`` to ``?sync=B`` (or dropping it) keeps
  // ViewerPage alive. So this effect also owns the session's lifetime: its
  // cleanup closes the outgoing session's socket and timers (see
  // teardownFollowerSession), and the body starts the incoming session from
  // clean state rather than inheriting A's connection status, presenter
  // banner or last view. A presenter-driven slide change does not touch
  // syncId, so it never reaches this cleanup and the socket survives it.
  useEffect(() => {
    setFollowerSlideRef(null)
    setFollowerRequireName(false)
    setFollowerConnState('connecting')
    setFollowerHasConnected(false)
    setEndedReason(null)
    setEndedNext(null)
    setFollowerSlideUnavailable(false)
    setPresenterAbsent(false)
    setPresenterGraceRemaining(null)
    if (!isFollower) {
      setFollowerStatus('idle')
      return teardownFollowerSession
    }
    let cancelled = false
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    setFollowerStatus('peek')
    // A peek the server could not answer right now is retried, staggered:
    // a class behind one NAT address opening the shared link together is one
    // client to the rate limiter, and the sync service may be restarting.
    // Shown as the error only once the retries run out.
    const peek = (attempt: number): Promise<Awaited<ReturnType<typeof peekSyncSession>>> =>
      peekSyncSession(syncId).catch((err) => {
        if (cancelled || !isSyncPeekUnavailableError(err) || attempt >= PEEK_RETRIES) throw err
        return new Promise((resolve, reject) => {
          retryTimer = setTimeout(() => {
            retryTimer = null
            peek(attempt + 1).then(resolve, reject)
          }, retryDelayMs(attempt))
        })
      })
    peek(0).then((info) => {
      if (cancelled) return
      if (!info) {
        setFollowerStatus('notfound')
        return
      }
      const slideInfo = { catalog_id: info.slide_catalog_id, title: info.slide_title }
      setFollowerSlideRef(slideInfo)
      setFollowerRequireName(!!info.require_name)
      // If the user opened the URL on the wrong slide, jump to the right
      // one before showing the dialog so the OSD viewer below loads the
      // session's slide on the first paint. Re-check cancelled right
      // before nav() so an unmount-before-resolve can't trigger a
      // navigation on a torn-down component. The navigation does not remount
      // this hook (slidePublicId is out of the deps), so it falls straight through
      // to 'join' without re-issuing the peek — read the live slide token via
      // slidePublicIdRef since this resolves asynchronously after the URL may
      // have changed.
      // Guard on a truthy token: the server can legitimately emit an empty
      // slide_public_id (e.g. the room's slide row was deleted), and navigating
      // to `/slides/` would eject the joiner to the index. Ignore it instead.
      if (info.slide_public_id && info.slide_public_id !== slidePublicIdRef.current) {
        if (cancelled) return
        const qs = new URLSearchParams(searchParamsRef.current)
        nav(`/slides/${encodeURIComponent(info.slide_public_id)}?${qs.toString()}`, { replace: true })
      }
      setFollowerStatus('join')
    }).catch((err) => {
      if (cancelled) return
      logger.error('peek sync session failed', err)
      setFollowerStatus('error')
    })
    return () => {
      cancelled = true
      if (retryTimer != null) clearTimeout(retryTimer)
      teardownFollowerSession()
    }
    // nav / searchParamsRef / slidePublicIdRef are intentionally omitted: the
    // effect must run once per session, not on every slide change (the slide
    // token is read via the ref). The rest are stable refs / setters, and
    // teardownFollowerSession touches refs only. ``peekRun`` re-runs it for
    // retryPeek.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [syncId, isFollower, peekRun])

  // The error overlay's retry: peek again from the start.
  function retryPeek() {
    setPeekRun((n) => n + 1)
  }

  // Release everything the outgoing session holds: its socket (including a
  // 4503 retry timer — SyncFollower.close() clears it), the presenter-absence
  // countdown, the pending animation-finish handler and the last view / cursor
  // frames. Bumping connGenRef first fences off any callback still in flight
  // from that socket. Runs from the session effect's cleanup, i.e. on a
  // ``?sync=`` change or removal and on unmount. Refs and DOM only: the React
  // state is reset by the next session's effect body (on unmount there is no
  // next session to reset for).
  function teardownFollowerSession() {
    connGenRef.current += 1
    if (graceTimerRef.current !== null) {
      clearInterval(graceTimerRef.current)
      graceTimerRef.current = null
    }
    try { followerSocketRef.current?.close() } catch { /* swallow */ }
    followerSocketRef.current = null
    if (animFinishCleanupRef.current) {
      try { animFinishCleanupRef.current() } catch { /* swallow */ }
      animFinishCleanupRef.current = null
    }
    // The old session's last view must not be replayed by ViewerPage's OSD
    // ``open`` handler for the new session, and its laser dot (image-pixel
    // coordinates from the old presenter) must not stay on screen.
    latestViewRef.current = null
    lastCursorRef.current = null
    if (cursorOverlayRef.current) cursorOverlayRef.current.style.display = 'none'
  }

  function applyViewFrame(frame: ViewFrame, immediately = false) {
    const v = osdRef.current
    if (!v || !v.viewport) return
    const [x, y, w, h] = frame.bounds
    try {
      // Only on a change: the presenter streams frames while the view moves,
      // and re-issuing an unchanged angle restarts a running rotation spring.
      const rotation = frame.rotation_deg || 0
      if (v.viewport.getRotation() !== rotation) v.viewport.setRotation(rotation, immediately)
      // Reconstruct the presenter's rect including its degrees, otherwise a
      // rotated frame fits the wrong region (b.x/b.y is the rotated corner).
      v.viewport.fitBounds(new OpenSeadragon.Rect(x, y, w, h, frame.bounds_deg || 0), immediately)
    } catch { /* swallow */ }
    // Remove any previously registered animation-finish handler before
    // adding a new one so rapid frames don't stack up handlers.
    if (animFinishCleanupRef.current) {
      animFinishCleanupRef.current()
      animFinishCleanupRef.current = null
    }
    const handler = () => {
      animFinishCleanupRef.current = null
    }
    v.addOnceHandler('animation-finish', handler)
    animFinishCleanupRef.current = () => v.removeHandler('animation-finish', handler)
  }

  function handleRemoteView(frame: ViewFrame) {
    // Store the frame BEFORE the slide check: on a slide change the OSD viewer
    // is rebuilt for the new slide, and its ``open`` handler (ViewerPage)
    // restores the viewport from ``latestViewRef.current``. Since this frame
    // already carries the new slide's bounds, the restore lands on the right
    // view on first paint.
    latestViewRef.current = frame
    // Only treat this as a slide change when the frame carries a real token;
    // a missing/empty one must not navigate the follower to `/slides/`.
    if (frame.slide_public_id && frame.slide_public_id !== slidePublicIdRef.current) {
      // The presenter switched slides. Update the URL so ViewerPage's slideId
      // changes and its OSD-init effect loads the new slide's tiles — but do
      // NOT touch followerSocketRef. ViewerPage is not remounted (the route has
      // no key), so the single follower socket stays attached; the server
      // already updated room.slide_id and fanned this frame out to this socket without
      // detaching. ``replace`` keeps the follower out of the back-history so
      // they can't navigate back to a previous slide and desync.
      //
      // Reset the cursor: its coordinates are image-pixel space tied to the
      // previous slide, and the server cleared room.last_cursor on the slide
      // change, so hide the laser dot until the presenter moves on the new
      // slide (otherwise a stale dot projects onto an unrelated location).
      lastCursorRef.current = null
      reprojectCursor()
      // Whatever was unavailable was the slide being left.
      setFollowerSlideUnavailable(false)
      const qs = new URLSearchParams(searchParamsRef.current)
      nav(`/slides/${encodeURIComponent(frame.slide_public_id ?? '')}?${qs.toString()}`, { replace: true })
      return
    }
    applyViewFrame(frame)
  }

  // Snap the viewer to the presenter's last frame, at once. Called by
  // ViewerPage when this slide's image opens (so the first paint is already
  // the presenter's view) and after the follower's own viewer resizes: OSD
  // keeps the old centre and scale on a resize, so a window resize or a
  // device rotation would otherwise crop the presenter's field until the
  // presenter next moved. Returns false when there is no frame for the slide
  // on screen, leaving the caller to its own fallback.
  function refitToLatestView(): boolean {
    const latest = latestViewRef.current
    if (!latest || (latest.slide_public_id ?? '') !== slidePublicIdRef.current) return false
    applyViewFrame(latest, true)
    return true
  }

  // ``smooth`` enables a short CSS transform transition so the dot glides
  // between the (15 Hz) presenter cursor frames instead of stepping. It is
  // applied ONLY when a new remote frame arrives — never on the update-viewport
  // path below, where the dot must track the follower's own pan/zoom animation
  // frame-for-frame; a transition there would smear the dot a step behind the
  // image on every viewport tick.
  function reprojectCursor(smooth = false) {
    const el = cursorOverlayRef.current
    if (!el) return
    const v = osdRef.current
    const last = lastCursorRef.current
    // Hidden, not left where it was, whenever there is no image to place it
    // on: while a slide switch is pending (the presenter's frames already name
    // the next slide, but the URL — and the viewer — have not caught up yet;
    // a reconnect replays the next slide's pointer right behind its view, and
    // it would be projected onto the slide still on screen), and between one
    // viewer's teardown and the next one's image opening.
    const latest = latestViewRef.current
    const switching = !!latest?.slide_public_id && latest.slide_public_id !== slidePublicIdRef.current
    if (!last || !last.visible || !v || !v.viewport || !v.isOpen() || switching) {
      el.style.display = 'none'
      return
    }
    try {
      const p = v.viewport.imageToViewerElementCoordinates(
        new OpenSeadragon.Point(last.x, last.y),
      )
      // 12 = half of the 24px overlay so the laser dot is centred on the
      // presenter's cursor location.
      el.style.transition = smooth ? 'transform 90ms linear' : 'none'
      el.style.display = 'block'
      el.style.transform = `translate3d(${p.x - 12}px, ${p.y - 12}px, 0)`
    } catch {
      el.style.display = 'none'
    }
  }

  function handleRemoteCursor(frame: CursorFrame) {
    lastCursorRef.current = frame
    reprojectCursor(true)
  }

  // Stop and reset the presenter-absence countdown. Called when the presenter
  // returns, when the session ends, and on leave/unmount so a lingering
  // interval can't keep ticking after the banner is gone.
  function clearPresenterAbsence() {
    if (graceTimerRef.current !== null) {
      clearInterval(graceTimerRef.current)
      graceTimerRef.current = null
    }
    setPresenterAbsent(false)
    setPresenterGraceRemaining(null)
  }

  // Handle a server `presenter` frame: present:false starts (or refreshes) the
  // local countdown from the server-provided grace seconds; present:true means
  // the presenter reattached, so clear the notice.
  function handlePresenter(present: boolean, graceSeconds?: number) {
    if (graceTimerRef.current !== null) {
      clearInterval(graceTimerRef.current)
      graceTimerRef.current = null
    }
    if (present) {
      setPresenterAbsent(false)
      setPresenterGraceRemaining(null)
      return
    }
    setPresenterAbsent(true)
    const start = Math.max(0, Math.floor(graceSeconds ?? 0))
    setPresenterGraceRemaining(start)
    graceTimerRef.current = window.setInterval(() => {
      setPresenterGraceRemaining((r) => {
        if (r === null || r <= 1) {
          // Hold at 0 and stop ticking; the server's `end` frame (fired at the
          // same deadline) drives the terminal 'ended' transition.
          if (graceTimerRef.current !== null) {
            clearInterval(graceTimerRef.current)
            graceTimerRef.current = null
          }
          return 0
        }
        return r - 1
      })
    }, 1000)
  }

  // Measure the follower's viewer-canvas size in CSS pixels for the hello
  // frame, so the presenter's audience panel can see who is on a
  // differently-shaped screen. The OSD *element* is what matters, not the
  // window or the physical display: it is the rect fitBounds pads to, so its
  // aspect ratio is exactly what decides how much extra margin the follower
  // sees around the presenter's field. Fall back to the window only while the
  // viewer is still being constructed (joinSession can run before OSD opens),
  // and return null rather than a fabricated size if neither is measurable.
  //
  // The fallback mirrors how ViewerPage actually sizes that element rather
  // than using innerHeight: the layout's height comes from useViewportHeight,
  // which prefers ``visualViewport.height`` precisely because iPadOS Safari
  // gets the full-height computation wrong on a client-side route's first
  // paint. Width stays on ``innerWidth`` (the layout viewport) since a
  // follower's column spans the full width — there is no presenter sidebar on
  // this side — and visualViewport.width would shrink under a pinch.
  function measureViewport(): { vw: number; vh: number } | null {
    const el = osdRef.current?.element
    if (el) {
      const r = el.getBoundingClientRect()
      if (r.width >= 1 && r.height >= 1) {
        return { vw: Math.round(r.width), vh: Math.round(r.height) }
      }
    }
    if (typeof window === 'undefined') return null
    const w = window.innerWidth
    const h = window.visualViewport?.height ?? window.innerHeight
    if (w >= 1 && h >= 1) return { vw: Math.round(w), vh: Math.round(h) }
    return null
  }

  // Open the WebSocket once the user clicks "Join" in the dialog. Called
  // exactly once per session (a presenter-driven slide change doesn't
  // rebuild the socket); the close-then-rebuild guard below only covers a
  // double-click race on the Join button.
  function joinSession(name: string | null) {
    clearPresenterAbsence()
    if (followerSocketRef.current) {
      try { followerSocketRef.current.close() } catch { /* swallow */ }
      followerSocketRef.current = null
    }
    // Claim a fresh generation BEFORE constructing: the constructor opens the
    // socket and notifies 'connecting' synchronously, before the assignment
    // to followerSocketRef below has happened, so an identity check against
    // the ref would wrongly reject that first notification.
    const gen = ++connGenRef.current
    const isCurrent = () => connGenRef.current === gen
    setFollowerStatus('connected')
    followerSocketRef.current = new SyncFollower({
      sessionId: syncId,
      name,
      getViewport: measureViewport,
      onView: (f) => { if (isCurrent()) handleRemoteView(f) },
      onCursor: (f) => { if (isCurrent()) handleRemoteCursor(f) },
      onEnd: (f) => {
        if (!isCurrent()) return
        clearPresenterAbsence()
        setEndedReason(f?.reason ?? null)
        setEndedNext(
          f?.reason === 'superseded' && f.next?.sid && f.next.slide_public_id ? f.next : null,
        )
        setFollowerStatus('ended')
        try { followerSocketRef.current?.close() } catch { /* swallow */ }
        followerSocketRef.current = null
      },
      onPresenter: (f) => { if (isCurrent()) handlePresenter(f.present, f.grace_seconds) },
      onNotice: (code) => {
        if (!isCurrent()) return
        if (code === 'slide_unavailable') setFollowerSlideUnavailable(true)
        else if (code === 'slide_available') setFollowerSlideUnavailable(false)
      },
      onStateChange: (s) => {
        if (!isCurrent()) return
        // A (re)seated socket starts from a clean presence state: the server
        // re-sends ``present: false`` on attach if the absence is still
        // announced, but a presenter who returned while this client was
        // disconnected produced a "returned" frame it never saw — without this
        // reset the stale "ending in 0s" banner would stick. 'open' is
        // notified on the first inbound frame, before that frame is handled,
        // so the re-sent absence notice (if any) lands after this clear — and
        // a socket the server refused never reports 'open', so it cannot
        // clear a countdown the server will not re-send.
        if (s === 'open') {
          clearPresenterAbsence()
          // Admitted, so the slide is published: a ``slide_available`` sent
          // while this socket was down is not lost on the banner.
          setFollowerSlideUnavailable(false)
        }
        setFollowerConnState(s)
      },
    })
  }

  function cancelJoin() {
    nav('/', { replace: true })
  }

  function leaveSession() {
    clearPresenterAbsence()
    try { followerSocketRef.current?.close() } catch { /* swallow */ }
    followerSocketRef.current = null
    setFollowerHasConnected(false)
    nav('/', { replace: true })
  }

  // Mark that the follower has successfully connected at least once so
  // subsequent attempts show "reconnecting" instead of "connecting".
  useEffect(() => {
    if (followerConnState === 'open') setFollowerHasConnected(true)
  }, [followerConnState])

  // 'gone' surfaces a server-side 4404 from sync.py — the room is gone
  // for good (server restart, GC, or an end-without-end-frame path). The
  // socket has already self-closed; transition the follower into the
  // 'ended' overlay so the user gets a clear terminal state instead of a
  // permanent "disconnected" banner.
  useEffect(() => {
    if (followerConnState !== 'gone') return
    clearPresenterAbsence()
    // 'full' too: a room that ends while this client is retrying for a slot
    // answers the retry with 4404.
    setFollowerStatus((s) => (s === 'connected' || s === 'full' ? 'ended' : s))
    try { followerSocketRef.current?.close() } catch { /* swallow */ }
    followerSocketRef.current = null
  }, [followerConnState])

  // 'full' surfaces a server-side 4503 from sync.py — the room is at its
  // FOLLOWERS_PER_ROOM_MAX cap, or this IP at its per-IP share. Show the
  // "room full" overlay, but leave the socket alone: SyncFollower keeps
  // retrying with backoff and reports 'open' only once a retry is seated,
  // which the effect below turns back into 'connected'.
  useEffect(() => {
    if (followerConnState !== 'full') return
    // Stop the presenter-absence grace countdown like the 'gone'/'unauthorized'
    // states do — otherwise its 1 Hz interval keeps firing behind the
    // "room full" overlay and a stale "presenter left — ending in Ns" banner
    // can render meanwhile.
    clearPresenterAbsence()
    setFollowerStatus((s) => (s === 'connected' || s === 'join' || s === 'peek' ? 'full' : s))
  }, [followerConnState])

  // A retry after 'full' got a slot.
  useEffect(() => {
    if (followerConnState !== 'open') return
    setFollowerStatus((s) => (s === 'full' ? 'connected' : s))
  }, [followerConnState])

  // 'unauthorized' surfaces a 4401 and 'rejected' a 4400 close from sync.py
  // (an app-access / sign-in rejection, or a name required) — both shown as
  // the follower's 'unauthorized' overlay — and 'forbidden' a 4403 (the IP filter
  // denies this address, or the page's origin was refused). sync.ts has
  // already stopped reconnecting, so without a terminal state the follower
  // would sit on a permanent "disconnected" banner with no explanation.
  // Surface a clear terminal overlay that names the cause instead.
  useEffect(() => {
    if (followerConnState !== 'unauthorized' && followerConnState !== 'rejected' && followerConnState !== 'forbidden') return
    const next: FollowerStatus = followerConnState === 'forbidden' ? 'forbidden' : 'unauthorized'
    clearPresenterAbsence()
    setFollowerStatus((s) => (s === 'connected' || s === 'join' || s === 'peek' || s === 'full' ? next : s))
    try { followerSocketRef.current?.close() } catch { /* swallow */ }
    followerSocketRef.current = null
  }, [followerConnState])

  // Report visibility changes so the presenter's audience panel knows when
  // the follower has the tab in the background.
  useEffect(() => {
    if (!isFollower) return
    function onVis() {
      followerSocketRef.current?.sendVisibility(document.visibilityState !== 'hidden')
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [isFollower])

  // No separate unmount effect: the session effect's cleanup above runs on
  // unmount as well and tears the socket and timers down there.

  return {
    followerStatus,
    followerSlideRef,
    followerRequireName,
    followerConnState,
    followerHasConnected,
    endedReason,
    endedNext,
    followerSlideUnavailable,
    presenterAbsent,
    presenterGraceRemaining,
    cursorOverlayRef,
    reprojectCursor,
    refitToLatestView,
    joinSession,
    cancelJoin,
    leaveSession,
    retryPeek,
  }
}
