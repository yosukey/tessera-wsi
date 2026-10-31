import { useEffect } from 'react'
import { recordSlideDwell } from '../lib/api'
import {
  createForegroundClock,
  createSessionTimestamps,
  makeSessionId,
  type ForegroundClock,
  type SessionTimestamps,
} from '../lib/analyticsTime'

// Per-mount dwell tracker: starts a foreground clock when the hook mounts
// with a real slide id and emits beacons keyed by a per-mount session_id.
// The visibility API (inside createForegroundClock) subtracts time spent in
// a backgrounded tab — leaving the browser open on another tab shouldn't
// count as "reading the slide".
//
// Why a session_id rather than firing exactly once: firing once (a `fired`
// flag on the first `visibilitychange === hidden`, never sending again) would
// record only the first 3 minutes of the common "read 3 min → switch tab →
// come back → read 5 min" flow. Every flush (hidden / pagehide / unmount)
// re-sends the running foreground total under the same session_id; the backend
// reconciles them into one row via max(dwell_ms), so the full foreground
// dwell is captured without ever double-counting.
//
// Tab-close coverage: React unmount only runs on SPA navigation, not on
// pagehide / browser-close, so the cleanup beacon alone loses the dwell when
// the user just closes the tab. Additional flushes fire via `pagehide`
// (desktop / modern Safari) and `visibilitychange === hidden` (mobile
// fallback where pagehide is unreliable).
//
// Heartbeat: the flushes above all hang off an event, and none fires when the
// browser crashes, the OS kills a frozen mobile tab, or the machine loses
// power — the whole visit's dwell would then be lost. A periodic flush bounds
// that loss to one interval. Repeats are safe for the same reason as above
// (max(dwell_ms) per session_id, a conditional UPDATE that only ever raises
// the stored value).
//
// Back/forward cache: a page put in the bfcache (pagehide with `persisted`)
// is frozen, not unloaded, and may be shown again much later. The session is
// ended there — flushed, its clock stopped — and a restore (pageshow with
// `persisted`) starts a fresh one, matching the fresh page view
// useAnalyticsBeacon records for the restore; carrying the old session on
// would stretch its started_at..ended_at span over the time spent frozen.
// Splitting the visit in two is harmless here: the ranking is by a slide's
// total dwell, summed over its sessions, so the two halves add up to the same
// total (the per-session average counts the restore as its own visit, as the
// page-view count does).
// useTourSession does the opposite and resumes its session across the cache:
// tour statistics count sessions (completion rate, per-session completion
// time), so a split would read as an abandoned tour plus a new one, or a
// second completion.
//
// Anonymous viewers and analytics_mode='off' deployments still call the
// beacon; the backend silently drops the row in either case so the
// frontend doesn't need to know the current mode at write time.

// Every beacon shares nginx's per-client /api/ budget (120 r/min) with the
// rest of the app, and a classroom behind one NAT address is one client: 40
// students reading at a 30 s heartbeat would spend 80 r/min on this alone. At
// two minutes that is 20 r/min, and a crash loses at most two minutes of a
// visit. A hidden tab's clock is paused (and was flushed on hiding), so its
// heartbeat sends nothing.
const HEARTBEAT_MS = 120_000
const MIN_DWELL_MS = 1500

type DwellSession = {
  id: string
  stamps: SessionTimestamps
  clock: ForegroundClock
}

function beginSession(): DwellSession {
  // `ended_at` comes from the same session stamps as `started_at`, so it can
  // never precede it even if the system clock is set back mid-session (the
  // server drops a dwell row whose end is before its start).
  const session = {
    id: makeSessionId(),
    stamps: createSessionTimestamps(),
    clock: createForegroundClock(),
  }
  session.clock.start()
  return session
}

// Keyed by the slide's public_id token (from the URL). The backend resolves
// the token to the internal id for storage.
export function useSlideDwell(slidePublicId: string | null | undefined): void {
  useEffect(() => {
    if (!slidePublicId) return

    // null while the page sits in the bfcache (between a persisted pagehide
    // and the pageshow that restores it).
    let session: DwellSession | null = beginSession()

    const flush = () => {
      if (!session) return
      const { clock, stamps, id } = session
      // Bank any in-flight foreground stretch first so `total()` reflects
      // the latest visible time (a no-op when the tab is already hidden —
      // the clock's own visibilitychange listener banked it already).
      clock.bank()
      const dwellMs = clock.total()
      // Skip the beacon for trivially short dwells: a quick bounce through
      // the page (slide list -> mis-click -> back) shouldn't contaminate
      // the average with sub-second rows. The session_id keeps repeated
      // flushes from this same mount idempotent on the server.
      if (dwellMs < MIN_DWELL_MS) return
      recordSlideDwell({
        slide_public_id: slidePublicId,
        session_id: id,
        started_at: stamps.startedAt,
        ended_at: stamps.nowIso(),
        dwell_ms: dwellMs,
      })
    }

    const endSession = () => {
      if (!session) return
      flush()
      session.clock.stop()
      session = null
    }

    const heartbeat = window.setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return
      flush()
    }, HEARTBEAT_MS)
    // A plain pagehide (the page is being unloaded) only needs the final
    // flush; a persisted one freezes the page, so the session ends with it.
    const onPagehide = (e: PageTransitionEvent) => {
      if (e.persisted) endSession()
      else flush()
    }
    const onPageshow = (e: PageTransitionEvent) => {
      if (e.persisted && !session) session = beginSession()
    }
    const onVisibility = () => {
      if (typeof document !== 'undefined' && document.hidden) flush()
    }
    window.addEventListener('pagehide', onPagehide)
    window.addEventListener('pageshow', onPageshow)
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      window.clearInterval(heartbeat)
      window.removeEventListener('pagehide', onPagehide)
      window.removeEventListener('pageshow', onPageshow)
      document.removeEventListener('visibilitychange', onVisibility)
      endSession()
    }
  }, [slidePublicId])
}
