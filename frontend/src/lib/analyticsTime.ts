// Shared timing helpers used by the per-session analytics beacons
// (useSlideDwell, useTourSession). Shared so both hooks emit the same
// ISO-with-offset format, the same "foreground-only" dwell accumulator and the
// same start/end timestamp rules.
//
// Two clocks, kept apart on purpose: elapsed time (dwell) is measured on the
// monotonic clock, and the wall clock is used only to stamp calendar times.
// The wall clock can be stepped at any moment (an NTP correction, the user
// setting the system time), so a duration computed as a difference of two
// wall readings can come out negative or inflated by the size of the step.

// Client-minted opaque session correlator, always an RFC 4122 v4 UUID.
// Shared by useTourSession and useSlideDwell so both per-session beacons
// key their incremental updates the same way. The backend validates the
// 8-4-4-4-12 shape (routers/analytics.py `_SESSION_ID_RE`) and drops anything
// else, so every branch must produce exactly that shape.
//
// crypto.randomUUID exists only in secure contexts (HTTPS / localhost);
// a plain-HTTP deployment falls through to getRandomValues, which is
// available everywhere, and Math.random is the last resort.
export function makeSessionId(): string {
  const cryptoObj = (typeof window !== 'undefined' ? window.crypto : undefined) as
    | (Crypto & { randomUUID?: () => string })
    | undefined
  if (typeof cryptoObj?.randomUUID === 'function') return cryptoObj.randomUUID()
  const bytes = new Uint8Array(16)
  if (typeof cryptoObj?.getRandomValues === 'function') {
    cryptoObj.getRandomValues(bytes)
  } else {
    for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256)
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40 // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80 // RFC 4122 variant
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`
}

// Monotonic milliseconds, for measuring elapsed time only. performance.now()
// never runs backwards and ignores wall-clock steps; its origin is arbitrary
// (page load), so its readings are meaningful only as differences and must
// never be formatted as a date. The Date.now() fallback is for non-browser
// callers only — every browser this app supports has performance.now().
function monotonicNow(): number {
  if (typeof performance !== 'undefined' && typeof performance.now === 'function') {
    return performance.now()
  }
  return Date.now()
}

// Local-time ISO (second precision) with a timezone offset, so the server can
// normalise to UTC regardless of the client's time zone.
function formatIsoWithOffset(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  const off = -d.getTimezoneOffset()
  const sign = off >= 0 ? '+' : '-'
  const h = pad(Math.floor(Math.abs(off) / 60))
  const m = pad(Math.abs(off) % 60)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}${sign}${h}:${m}`
}

// Calendar timestamps for one analytics session: `startedAt` is fixed at
// creation, and `nowIso()` stamps a later moment of the same session (a slide
// dwell's `ended_at`, a tour's `completed_at` or a progress beacon's
// `sent_at`). The server measures a session's span between these stamps, not
// against its own clock (which only rejects stamps more than a week off), so a
// client clock that is minutes or hours off still yields the right span.
//
// `nowIso()` is the current wall-clock time, floored at `startedAt` plus the
// monotonic time elapsed since. In the normal case that floor is below the
// wall clock and changes nothing — including after system sleep, which the
// wall clock counts but a monotonic clock may not. It takes over only once the
// wall clock has been set back: without it the end would land before the
// start, and the server discards such rows outright (a slide dwell with
// ended_at < started_at, a tour completion with completed_at < started_at).
// A wall clock set *forward* mid-session is taken as the corrected time; the
// dwell itself is unaffected either way, as it comes from ForegroundClock.
export type SessionTimestamps = {
  startedAt: string
  nowIso(): string
}

export function createSessionTimestamps(): SessionTimestamps {
  const wallStart = Date.now()
  const monoStart = monotonicNow()
  return {
    startedAt: formatIsoWithOffset(new Date(wallStart)),
    nowIso(): string {
      const floor = wallStart + Math.max(0, monotonicNow() - monoStart)
      return formatIsoWithOffset(new Date(Math.max(Date.now(), floor)))
    },
  }
}

// Accumulates milliseconds spent with the document visible, banking each
// foreground stretch when the tab is hidden and resuming on visibilitychange.
// Holding the running clock in a closure keeps the consumer hook free of
// repeated visibilitychange bookkeeping. Every stretch is measured on the
// monotonic clock (see monotonicNow), so a wall-clock change mid-session can
// neither drive the total negative nor add the size of the jump to it.
export type ForegroundClock = {
  total(): number
  bank(): void          // bank any in-flight foreground stretch into `total`
  start(): void         // begin counting foreground time (call at mount)
  stop(): void          // detach the visibilitychange listener (call at cleanup)
  // Stop counting without ending the clock: banks the in-flight stretch, and
  // nothing accrues until resume() (or the tab next becoming visible). For a
  // page entering the back/forward cache, which may not be hidden yet when its
  // persisted pagehide fires — the time it spends frozen is not foreground
  // time, whatever order the browser fires the events in.
  pause(): void
  // Count again from now if the document is visible (a no-op when already
  // counting, e.g. a visibilitychange to visible ran first on a restore).
  resume(): void
}

export function createForegroundClock(): ForegroundClock {
  let accumMs = 0
  // null means "currently paused / hidden"; otherwise the monotonicNow()
  // reading at which the active foreground stretch began. A null sentinel
  // rather than 0, since performance.now() starts at 0 on page load.
  let activeSince: number | null = null
  // Clamped at 0 for the Date.now() fallback, the only source that can step
  // backwards.
  const stretchMs = (since: number) => Math.max(0, monotonicNow() - since)

  const onVisibility = () => {
    if (typeof document === 'undefined') return
    if (document.hidden) {
      if (activeSince !== null) {
        accumMs += stretchMs(activeSince)
        activeSince = null
      }
    } else if (activeSince === null) {
      activeSince = monotonicNow()
    }
  }

  return {
    total(): number {
      // Snapshot the in-flight stretch without committing it, so callers
      // can read the running total mid-session (e.g. for progress beacons)
      // without disrupting the bank/resume cycle.
      if (activeSince !== null && (typeof document === 'undefined' || !document.hidden)) {
        return accumMs + stretchMs(activeSince)
      }
      return accumMs
    },
    bank(): void {
      if (activeSince !== null) {
        const now = monotonicNow()
        accumMs += Math.max(0, now - activeSince)
        activeSince = now
      }
    },
    start(): void {
      accumMs = 0
      activeSince = (typeof document !== 'undefined' && document.hidden) ? null : monotonicNow()
      if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', onVisibility)
      }
    },
    stop(): void {
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility)
      }
      if (activeSince !== null) {
        accumMs += stretchMs(activeSince)
        activeSince = null
      }
    },
    pause(): void {
      if (activeSince !== null) {
        accumMs += stretchMs(activeSince)
        activeSince = null
      }
    },
    resume(): void {
      if (activeSince !== null) return
      if (typeof document !== 'undefined' && document.hidden) return
      activeSince = monotonicNow()
    },
  }
}
