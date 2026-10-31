// A short elapsed time — "42s", "5m", "1h 20m" in English; "42秒", "5分",
// "1時間20分" in Japanese — for the live-sync panels (how long a follower has
// been in the session, how old a session is). ``seconds`` is already rounded
// the way the caller wants; negatives (clock skew) read as 0.
export function formatElapsed(
  seconds: number,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return t('common.elapsedSeconds', { s })
  const m = Math.floor(s / 60)
  if (m < 60) return t('common.elapsedMinutes', { m })
  const h = Math.floor(m / 60)
  const rem = m % 60
  return rem ? t('common.elapsedHoursMinutes', { h, m: rem }) : t('common.elapsedHours', { h })
}
