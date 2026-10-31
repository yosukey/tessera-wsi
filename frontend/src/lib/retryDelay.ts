// Waits between retries of a request a whole class sends at the same instant —
// a live-sync follower's slide fetch when the presenter switches slides, its
// session peek when the shared link is opened — each spread by 0.5×–1.5× so a
// classroom's worth of clients refused together (one NAT address is one client
// to the rate limiter) comes back staggered; the cap keeps a long queue from
// leaving anyone waiting too long.
const RETRY_MS = [1_000, 2_000, 4_000, 8_000, 15_000]

export function retryDelayMs(attempt: number): number {
  const base = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)]
  return Math.round(base * (0.5 + Math.random()))
}
