import { useEffect, useRef } from 'react'
import { recordTourEvent, recordTourStart } from '../lib/api'
import {
  createForegroundClock,
  createSessionTimestamps,
  ForegroundClock,
  makeSessionId,
  SessionTimestamps,
} from '../lib/analyticsTime'

// Tour-session tracker. Manages a single session_id for the lifetime of
// the tour route, sending:
//   * one "start" event when the hook first sees a valid (tourPublicId,
//     totalSteps) pair,
//   * one "progress" event whenever the current step index advances,
//   * one "complete" event when the user lands on the final step,
//   * one "complete" or "progress" event on unmount, so an abandoned
//     tour still records "user got to step N before quitting" via
//     sendBeacon.
//
// The "complete" event is debounced to exactly once per session: a
// user who flips back and forth between the last step and earlier ones
// after completing it doesn't get re-counted.
//
// `total_dwell_ms` counts foreground time only (the visibility API is
// used to bank stretches when the tab is hidden) so an idle tab left
// open over lunch doesn't inflate per-tour completion averages.
//
// Back/forward cache: a page put in the bfcache (pagehide with `persisted`)
// is frozen, not unloaded, and may be shown again much later. Unlike
// useSlideDwell, which ends its session there and starts a fresh one on the
// restore, a tour session is *resumed*: the persisted pagehide flushes
// progress (as any pagehide does — the page may never come back) and pauses
// the foreground clock; the restore (pageshow with `persisted`) restarts the
// clock under the same session_id — no new "start", and no second "complete"
// (completedRef survives). The difference is in what the two tables count. A
// slide's dwell is a sum over its sessions, so splitting one visit in two
// changes nothing; a tour's statistics count sessions — completion rate is
// completed sessions / sessions, average completion time is per completed
// session — so a split would turn one reading of a tour into an incomplete
// session plus a new one (lowering the completion rate), and a restore on the
// final step would add a second "complete" with a near-zero completion time.
//
// What the server makes of the resumed session (routers/analytics.py, tour
// event handler): later progress / complete beacons for the session_id are
// accepted as for any open session, its started_at stays the original one,
// and each sent_at / completed_at is anchored to the server's receipt time.
// The frozen stretch therefore widens the span the server caps the dwell by,
// but the dwell itself is the foreground clock's, which did not run while
// frozen — so it is not inflated. A completion after a mid-tour restore does
// count the frozen time in the completion time, just as a tab left in the
// background before the last step does (the server caps it at the dwell
// ceiling either way).

export function useTourSession(
  tourPublicId: string | null | undefined,
  currentStepIdx: number,
  totalSteps: number,
): void {
  const sessionIdRef = useRef<string | null>(null)
  const clockRef = useRef<ForegroundClock | null>(null)
  // The current session's timestamps. `completed_at` and each progress
  // beacon's `sent_at` are taken from the same stamps as `started_at`, so a
  // system clock set back mid-tour can't put them before the start (the
  // server ignores such a completion), and the server can bound a reported
  // dwell by the session's span on the client's own clock, whatever its
  // offset from the server's.
  const stampsRef = useRef<SessionTimestamps | null>(null)
  const completedRef = useRef<boolean>(false)
  const lastReportedStepRef = useRef<number>(-1)
  const tourPublicIdRef = useRef<string | null>(null)
  // Mirror of the live `totalSteps` prop so the cleanup / pagehide
  // flushers (which only depend on `tourPublicId`) can clamp against the
  // latest step count without re-subscribing on every prop change.
  //
  // Only a positive count is mirrored. 0 means "not loaded": TourViewerPage
  // passes it (with a null id) while the route's tour data is in flight, and
  // the render that switches tours already carries it — the same commit in
  // which the outgoing session's cleanup sends its final progress snapshot.
  // Mirroring the 0 would clamp that snapshot to step 0. A session only
  // starts on a positive count, so the mirror always holds the live session's
  // latest known count.
  const totalStepsRef = useRef<number>(0)
  if (totalSteps > 0) totalStepsRef.current = totalSteps
  // The current session's in-flight "start" request, and whether it has
  // settled. The server drops progress/complete for a session_id whose row
  // does not exist yet, and separate requests are not delivered in order, so
  // every later event for the session waits on this (see `afterStart`).
  const startPromiseRef = useRef<Promise<void> | null>(null)
  const startSettledRef = useRef<boolean>(false)
  // Largest total_dwell_ms handed to the server for the current session.
  const reportedDwellRef = useRef<number>(0)

  // Run `send` once the current session's "start" has settled. Once it has,
  // `send` runs synchronously — which matters for the pagehide flush, where a
  // deferred callback may never get to run. Callers capture their payload
  // before calling, so a chained send still reports the session it was
  // queued for even if the tour changes meanwhile.
  const afterStart = (send: () => void) => {
    const p = startPromiseRef.current
    if (!p || startSettledRef.current) send()
    else void p.then(send)
  }

  // Tour change → cleanup the previous tour's session and reset. The
  // cleanup runs on unmount and on tourPublicId change; depending only on
  // tourPublicId here means an admin re-saving the tour in another tab (which
  // makes `totalSteps` update from N to N±1) won't tear down the active
  // session and mint a fresh one — that would create a duplicate row
  // with completed=false and a tiny dwell.
  //
  // Also subscribes to `pagehide` / `visibilitychange === hidden` so a
  // tab-close (which never triggers React unmount) still gets a final
  // progress beacon out. The progress endpoint is idempotent — it only
  // moves `last_step_index` forward and takes max() of `total_dwell_ms`
  // — so re-firing across pagehide and a later unmount is harmless.
  useEffect(() => {
    if (!tourPublicId) return
    tourPublicIdRef.current = tourPublicId

    const flushProgress = () => {
      const sid = sessionIdRef.current
      const tid = tourPublicIdRef.current
      const clock = clockRef.current
      const stamps = stampsRef.current
      if (!sid || !tid || !clock || !stamps) return
      // Clamp against the *current* totalStepsRef so a mid-session shrink
      // (admin removed steps in another tab) doesn't surface an index
      // beyond `total_steps`. Falls back to 0 when no step has been
      // reported yet.
      const last = lastReportedStepRef.current
      const cap = Math.max(0, totalStepsRef.current - 1)
      const stepIdx = last < 0 ? 0 : Math.min(last, cap)
      const totalDwell = clock.total()
      // After "complete" the session keeps accruing dwell (the reader lingers on
      // the last step, which is usually the longest read). The server takes the
      // max() of total_dwell_ms whether or not the session is completed, so a
      // trailing progress beacon records it; skip it only when nothing new has
      // accrued since the last report.
      if (completedRef.current && totalDwell <= reportedDwellRef.current) return
      reportedDwellRef.current = Math.max(reportedDwellRef.current, totalDwell)
      const sentAt = stamps.nowIso()
      afterStart(() => recordTourEvent({
        event: 'progress',
        session_id: sid,
        tour_public_id: tid,
        step_index: stepIdx,
        total_dwell_ms: totalDwell,
        sent_at: sentAt,
      }))
    }

    const endSession = () => {
      const clock = clockRef.current
      // Send the final progress snapshot before tearing the clock down;
      // the server-side max() on total_dwell_ms reconciles with any
      // earlier pagehide flush.
      flushProgress()
      // Clear so the next session starts fresh (the start effect below
      // gates on `sessionIdRef.current` being null).
      sessionIdRef.current = null
      clockRef.current = null
      stampsRef.current = null
      if (clock) clock.stop()
    }

    // Every pagehide flushes: an unloading page is going away, and a frozen
    // (persisted) one may be evicted from the cache without another event.
    // The frozen one also pauses the clock, and its restore resumes it — the
    // session itself carries on (see the header on the back/forward cache).
    const onPagehide = (e: PageTransitionEvent) => {
      flushProgress()
      if (e.persisted) clockRef.current?.pause()
    }
    const onPageshow = (e: PageTransitionEvent) => {
      if (e.persisted) clockRef.current?.resume()
    }
    const onVisibility = () => {
      if (typeof document !== 'undefined' && document.hidden) flushProgress()
    }
    window.addEventListener('pagehide', onPagehide)
    window.addEventListener('pageshow', onPageshow)
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      window.removeEventListener('pagehide', onPagehide)
      window.removeEventListener('pageshow', onPageshow)
      document.removeEventListener('visibilitychange', onVisibility)
      endSession()
    }
  }, [tourPublicId])

  // Send the "start" event once per (tourPublicId) lifetime, as soon as a
  // positive totalSteps is known (a bfcache restore resumes the session, so
  // it sends none). If totalSteps later changes while the
  // same tour is still mounted, leave the existing session alone — the
  // server's start handler is also idempotent on session_id, so there
  // is nothing more to send.
  useEffect(() => {
    if (!tourPublicId) return
    if (totalSteps <= 0) return
    if (sessionIdRef.current) return

    sessionIdRef.current = makeSessionId()
    completedRef.current = false
    lastReportedStepRef.current = -1
    reportedDwellRef.current = 0
    clockRef.current = createForegroundClock()
    clockRef.current.start()
    stampsRef.current = createSessionTimestamps()

    startSettledRef.current = false
    const p = recordTourStart({
      event: 'start',
      session_id: sessionIdRef.current,
      tour_public_id: tourPublicId,
      started_at: stampsRef.current.startedAt,
      step_index: 0,
      total_steps: totalSteps,
    })
    startPromiseRef.current = p
    void p.then(() => {
      if (startPromiseRef.current === p) startSettledRef.current = true
    })
  }, [tourPublicId, totalSteps])

  // Report step transitions and completion. This effect runs in the same
  // commit as the start effect above, so a single-step tour (or one opened
  // at ?step=<last>) produces "complete" immediately; `afterStart` holds it
  // until the "start" request has settled, otherwise it could reach the
  // server before the session row exists and be dropped.
  useEffect(() => {
    const sid = sessionIdRef.current
    const tid = tourPublicIdRef.current
    const clock = clockRef.current
    const stamps = stampsRef.current
    if (!sid || !tid || !clock || !stamps) return
    if (currentStepIdx < 0 || currentStepIdx >= totalSteps) return
    if (currentStepIdx <= lastReportedStepRef.current) return
    lastReportedStepRef.current = currentStepIdx

    const totalDwell = clock.total()
    reportedDwellRef.current = Math.max(reportedDwellRef.current, totalDwell)
    const isFinal = currentStepIdx === totalSteps - 1

    if (isFinal && !completedRef.current) {
      completedRef.current = true
      const completedAt = stamps.nowIso()
      afterStart(() => recordTourEvent({
        event: 'complete',
        session_id: sid,
        tour_public_id: tid,
        completed_at: completedAt,
        step_index: currentStepIdx,
        total_dwell_ms: totalDwell,
      }))
    } else {
      const sentAt = stamps.nowIso()
      afterStart(() => recordTourEvent({
        event: 'progress',
        session_id: sid,
        tour_public_id: tid,
        step_index: currentStepIdx,
        total_dwell_ms: totalDwell,
        sent_at: sentAt,
      }))
    }
  }, [currentStepIdx, totalSteps])
}
