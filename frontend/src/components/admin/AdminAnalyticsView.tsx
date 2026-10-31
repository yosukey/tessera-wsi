import React, { useEffect, useMemo, useRef, useState } from 'react'
import {
  AnalyticsRange,
  AnalyticsSummary,
  CsvDataset,
  TopSlideRow,
  TourCompletionRow,
  SlideDwellRow,
  adminGetTourCompletion,
  adminGetSlideDwell,
  TopTourRow,
  ReferrerRow,
  UaRow,
  TopUserRow,
  RecentPageView,
  PageViewKind,
  ActorRoleFilter,
  ServerTz,
  adminGetAnalyticsDashboard,
  adminGetRecentPageViews,
  adminClearAnalytics,
  adminClearAnalyticsUser,
  adminPreviewAnalyticsUser,
  adminPseudonymizeAnalytics,
  adminDownloadAnalyticsCsv,
  ANALYTICS_PREVIEW_SUBJECT_CAP,
  AnalyticsClearScope,
  httpStatusOf,
  isAbortError,
  isAdminFromToken,
  isApiCodeError,
  isRateLimitError,
  isUnauthorizedError,
} from '../../lib/api'
import { Locale, useI18n } from '../../lib/i18n'
import { apiErrorMessage, apiErrorMessageOr } from '../../lib/admin/apiError'
import type { TFn } from '../../lib/admin/validation'
import { formatInServerTz, serverTzLabel, todayInServerTz } from '../../lib/admin/datetime'
import AdminAnalyticsClearModal from './AdminAnalyticsClearModal'

// Cooldown between consecutive "refresh" clicks. A short cooldown is
// enough to keep a user mashing the button under nginx's 120 r/m budget;
// the button is also `disabled={loading}` while a request is actually
// in flight, so this only matters when responses return very fast.
const REFRESH_COOLDOWN_MS = 1000

// Debounce window for the recent-log free-text filters. A user typing
// "192.168.1.10" would otherwise fire 12 /recent requests in a few
// hundred milliseconds — enough to single-handedly trip the admin
// rate limit when other admin tabs already loaded their initial data.
const FILTER_DEBOUNCE_MS = 300

// Upper bound on a custom date-range span. Mirrors the backend's
// `_MAX_WINDOW_DAYS` so the client can surface an inline error instead
// of round-tripping to a 400.
const MAX_WINDOW_DAYS = 365

// Rows the recent-activity log asks for. A response this long has probably
// been cut short, which the log then says (the CSV export goes further).
const RECENT_LIMIT = 200

// Quick-select buttons. Adding/removing entries here is the only edit
// needed when the operator wants different defaults.
const PRESET_DAYS = [7, 30, 90] as const
type PresetDays = (typeof PRESET_DAYS)[number]
const PRESET_LABEL_KEY: Record<PresetDays, string> = {
  7: 'analytics.range7',
  30: 'analytics.range30',
  90: 'analytics.range90',
}

// The coded failures a destructive endpoint answers AFTER it may have
// committed slices of its own request (it deletes / rewrites in committed
// chunks for a bounded time, then fails with 500).
const PARTIAL_FAILURE_CODES = new Set([
  'adminDash.apiErr.analyticsDeleteFailed',
  'adminDash.apiErr.analyticsPseudonymizeFailed',
])

// Whether the request a destructive action failed on may still have changed
// rows. Only an outright refusal is known to have changed nothing: a rate
// limit (nginx answers before the app sees it), a rejected session / role, or
// a coded 4xx the server answers before any work (no hashing salt, no clear
// scope). A 5xx, a dropped connection or an uncoded error may follow
// committed work, so the dashboard is refreshed for those.
//
// The session / role rejection is read off the status, not the error class:
// only a rejected staff token becomes UnauthorizedError, while require_admin's
// 403 "Forbidden" (a manager / instructor whose token is fine) and any other
// 401 arrive as a plain Error that only its status (httpStatusOf) tells apart
// from the uncoded 500 above. Both are answered by the auth dependency before
// the handler runs.
function mayHaveCommitted(e: unknown): boolean {
  if (isRateLimitError(e) || isUnauthorizedError(e)) return false
  const status = httpStatusOf(e)
  if (status === 401 || status === 403) return false
  if (isApiCodeError(e) && !PARTIAL_FAILURE_CODES.has(e.code)) return false
  return true
}

// A destructive action's failure for the banner. A role rejection's message
// is the bare English "Forbidden", so it is said in the UI's words instead —
// with "nothing was changed", which mayHaveCommitted holds for it. Everything
// else is apiErrorMessage's (falling back to `fallbackKey` when that is
// empty), or apiErrorMessageOr's where `orFallback` asks for `fallbackKey` in
// place of an uncoded error's raw English server text.
//
// The server's IP filter answers 403 too ("Access denied", main.py
// ip_filter_middleware — the admin's address fell out of the allowed set
// meanwhile), before any auth or handler runs, so it also changed nothing;
// said as "only administrators" it would send an administrator looking at roles.
// Its detail text is the one thing that tells the two apart.
function actionErrorMessage(e: unknown, t: TFn, fallbackKey: string, orFallback = false): string {
  if (httpStatusOf(e) === 403) {
    return t((e as Error)?.message === 'Access denied' ? 'analytics.ipDenied' : 'analytics.forbidden')
  }
  if (orFallback) return apiErrorMessageOr(e, t, fallbackKey)
  return apiErrorMessage(e, t) || t(fallbackKey)
}

function fmt(n: number, locale: Locale): string {
  // Pin the BCP-47 tag to the UI locale so a user with a third-locale
  // browser (e.g. de-DE) doesn't see numbers in `1.234` style on an
  // English/Japanese UI where the rest of the layout is `1,234`.
  return n.toLocaleString(locale === 'ja' ? 'ja-JP' : 'en-US')
}

// A 0..1 ratio as a whole percentage, pinned to the UI locale like `fmt`.
function fmtPercent(ratio: number, locale: Locale): string {
  return new Intl.NumberFormat(locale === 'ja' ? 'ja-JP' : 'en-US', {
    style: 'percent',
    maximumFractionDigits: 0,
  }).format(Number.isFinite(ratio) ? ratio : 0)
}

// A table cell's title as a link to the public page, or as plain text when
// there is no page to link to: a deleted slide / tour keeps its rows but
// reports an empty public_id, and a link built from it would point at the
// bare /slides/ list (the recent log below applies the same rule).
function MaybeLink({ href, children }: { href: string | null; children: React.ReactNode }) {
  return href
    ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
    : <span>{children}</span>
}

const slideHref = (publicId: string) => (publicId ? `/slides/${publicId}` : null)
const tourHref = (slidePublicId: string, tourPublicId: string) =>
  (slidePublicId && tourPublicId ? `/slides/${slidePublicId}/tours/${tourPublicId}` : null)

// A Pseudonymous-mode identifier or IP as stored: the server's keyed-hash
// digest, 64 lowercase hex characters (analytics.py DIGEST_RE). One unbroken
// word that long, in a narrow table column, takes the width the other columns
// needed — in the recent log it would squeeze the page column until Japanese titles
// wrapped one character per line. Such a digest is shown as its first
// DIGEST_PREFIX characters, ample to tell rows apart by eye; the full value
// stays in the cell's title and is what a click puts in the filter. A raw IP
// or name (Identified mode) is shown whole.
const DIGEST_RE = /^[0-9a-f]{64}$/
const DIGEST_PREFIX = 12
function shortDigest(value: string): string {
  return DIGEST_RE.test(value) ? `${value.slice(0, DIGEST_PREFIX)}…` : value
}

// Compact duration for the tour-completion / slide-dwell tables. ms →
// "12s" / "1m23s" / "2h05m" depending on magnitude. Drops the smaller
// unit once the larger one is dominant so the column stays narrow.
function formatDurationMs(ms: number): string {
  if (!ms || ms < 0) return '0s'
  const totalSec = Math.round(ms / 1000)
  if (totalSec < 60) return `${totalSec}s`
  const min = Math.floor(totalSec / 60)
  const sec = totalSec % 60
  if (min < 60) return sec === 0 ? `${min}m` : `${min}m${String(sec).padStart(2, '0')}s`
  const hr = Math.floor(min / 60)
  const minRem = min % 60
  return `${hr}h${String(minRem).padStart(2, '0')}m`
}

// "YYYY-MM-DD" → "Mon" / "月" etc. The bucket key is a calendar date in
// the server's local TZ; the weekday is purely a calendar property, so
// Intl is pinned to UTC to avoid double-shifting (the input is already a
// date, not an instant — without timeZone:'UTC' negative-UTC browsers
// would render the previous weekday at the day boundary).
function weekdayShort(ymd: string, locale: Locale): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(ymd)
  if (!m) return ''
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])))
  if (isNaN(d.getTime())) return ''
  try {
    return new Intl.DateTimeFormat(locale === 'ja' ? 'ja-JP' : 'en-US', {
      weekday: 'short',
      timeZone: 'UTC',
    }).format(d)
  } catch {
    return ''
  }
}

function SubHeading({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', color: 'var(--color-text-faint)', margin: '14px 0 4px' }}>
      {children}
    </div>
  )
}

function ActorRoleBadge({ role }: { role: 'admin' | 'manager' | 'instructor' }) {
  const { t } = useI18n()
  const palette: Record<string, { bg: string; fg: string }> = {
    admin: { bg: 'var(--color-brand-50)', fg: 'var(--color-brand-700)' },
    manager: { bg: 'var(--color-success-bg)', fg: 'var(--color-success-fg)' },
    instructor: { bg: 'var(--color-info-bg)', fg: 'var(--color-info-fg)' },
  }
  const c = palette[role]
  return (
    <span style={{
      display: 'inline-block',
      padding: '1px 6px',
      borderRadius: 10,
      background: c.bg,
      color: c.fg,
      fontSize: 10,
      fontWeight: 600,
    }}>{t(`roles.${role}`)}</span>
  )
}

function SummaryCard({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <div style={{ flex: '1 1 140px', minWidth: 140, padding: '10px 12px', border: '1px solid var(--color-border)', borderRadius: 6, background: 'var(--color-surface)' }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', color: 'var(--color-text-faint)' }}>{label}</div>
      <div style={{ fontSize: 20, fontFamily: 'monospace', marginTop: 2 }}>{value}</div>
      {sub && <div style={{ fontSize: 10, opacity: 0.6, marginTop: 1 }}>{sub}</div>}
    </div>
  )
}

// Drop leading days that have no recorded traffic. On a fresh install the
// backend's zero-fill emits a full 30-day window with only "today" populated;
// rendering 29 empty bars wastes vertical space and reads as a bug. Trailing
// and middle zeros are preserved so a recent drop-off in traffic still shows.
function trimLeadingZeroDays(daily: AnalyticsSummary['daily']): AnalyticsSummary['daily'] {
  const firstNonZero = daily.findIndex(r => r.top + r.viewer + r.tour > 0)
  return firstNonZero < 0 ? [] : daily.slice(firstNonZero)
}

function DailyChart({ daily, locale }: { daily: AnalyticsSummary['daily']; locale: Locale }) {
  const { t } = useI18n()
  const maxTotal = useMemo(
    () => daily.reduce((m, r) => Math.max(m, r.top + r.viewer + r.tour), 0) || 1,
    [daily],
  )
  if (daily.length === 0) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 11, fontFamily: 'monospace' }}>
      {daily.map(row => {
        const total = row.top + row.viewer + row.tour
        // `maxTotal` is clamped to 1 above so division is always safe;
        // when a row's component count is 0 that segment naturally
        // collapses to 0% width.
        const pctTop = (row.top / maxTotal) * 100
        const pctViewer = (row.viewer / maxTotal) * 100
        const pctTour = (row.tour / maxTotal) * 100
        const wd = weekdayShort(row.day, locale)
        return (
          <div key={row.day} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ width: 110, textAlign: 'right', opacity: 0.7 }}>
              {row.day}
              {wd && <span style={{ marginLeft: 4, opacity: 0.7 }}>({wd})</span>}
            </span>
            {/* The per-kind counts are otherwise only in the segments' hover
                titles, which neither keyboard nor screen-reader users get;
                the bar as a whole carries them as its accessible name. */}
            <div
              role="img"
              aria-label={`${row.day}: ${t('analytics.kindTop')} ${fmt(row.top, locale)}, ${t('analytics.kindViewer')} ${fmt(row.viewer, locale)}, ${t('analytics.kindTour')} ${fmt(row.tour, locale)}`}
              style={{ flex: 1, display: 'flex', height: 14, background: 'var(--color-surface-alt, #f5f5f5)', borderRadius: 2, overflow: 'hidden' }}
            >
              <div title={`${t('analytics.kindTop')} ${fmt(row.top, locale)}`} style={{ width: `${pctTop}%`, background: '#88c' }} />
              <div title={`${t('analytics.kindViewer')} ${fmt(row.viewer, locale)}`} style={{ width: `${pctViewer}%`, background: '#6a6' }} />
              <div title={`${t('analytics.kindTour')} ${fmt(row.tour, locale)}`} style={{ width: `${pctTour}%`, background: '#c96' }} />
            </div>
            <span style={{ width: 56, textAlign: 'right' }}>{fmt(total, locale)}</span>
          </div>
        )
      })}
    </div>
  )
}

const TH: React.CSSProperties = {
  textAlign: 'left',
  padding: '4px 6px',
  borderBottom: '1px solid var(--color-border)',
  fontSize: 10,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  color: 'var(--color-text-faint)',
}
const TD: React.CSSProperties = {
  padding: '4px 6px',
  borderBottom: '1px solid var(--color-border)',
  fontSize: 12,
  verticalAlign: 'top',
}
// Content-width floor of the recent log's page column: six full-width
// characters at TD's 12px, plus slack (see the column's header).
const PAGE_COL_MIN_PX = 96

export default function AdminAnalyticsView() {
  const { t, locale } = useI18n()
  // `range` is the live selection; `customStart` / `customEnd` are the
  // raw date-input values, which only become the active range once the
  // user clicks "Apply" so a half-typed date doesn't fire a fetch.
  // `customOpen` shows those inputs: opening them leaves `range` (and so the
  // data on screen) on the preset until Apply.
  const [range, setRange] = useState<AnalyticsRange>({ mode: 'preset', days: 30 })
  const [customOpen, setCustomOpen] = useState(false)
  // Seeded from the browser's date until the dashboard reports the server's
  // analytics zone; switching to the custom range re-seeds in that zone.
  const [customStart, setCustomStart] = useState<string>(() => todayInServerTz(null, 29))
  const [customEnd, setCustomEnd] = useState<string>(() => todayInServerTz(null))
  const [customError, setCustomError] = useState<string>('')
  const [csvBusy, setCsvBusy] = useState<CsvDataset | null>(null)
  // The raw `recent` rows are the most general export (every aggregate
  // shown on the dashboard can be derived from them), so it's the default.
  // The selector lets an operator grab the pre-aggregated tables too.
  const [csvDataset, setCsvDataset] = useState<CsvDataset>('recent')
  // The export's own failure, shown beside its button. Not `err`: a download
  // starting would wipe the dashboard's load failure (leaving the emptied
  // dashboard reading as "no traffic"), and the dashboard's next fetch, which
  // empties `err` as it starts, would wipe an export failure. Kept until the
  // next export starts.
  const [csvErr, setCsvErr] = useState('')
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null)
  const [topSlides, setTopSlides] = useState<TopSlideRow[]>([])
  const [topTours, setTopTours] = useState<TopTourRow[]>([])
  const [referrers, setReferrers] = useState<ReferrerRow[]>([])
  const [ua, setUa] = useState<UaRow[]>([])
  const [topUsers, setTopUsers] = useState<TopUserRow[]>([])
  const [tourCompletion, setTourCompletion] = useState<TourCompletionRow[]>([])
  const [slideDwell, setSlideDwell] = useState<SlideDwellRow[]>([])
  const [recent, setRecent] = useState<RecentPageView[]>([])
  // The recent log has its own request, so its own spinner and error: the
  // shared `err` banner belongs to the dashboard block, and a log failure
  // written there would outlive the next successful log fetch.
  const [recentLoading, setRecentLoading] = useState(false)
  const [recentErr, setRecentErr] = useState('')
  const [serverTz, setServerTz] = useState<ServerTz | null>(null)
  // `inputUserEmail` / `inputClientIp` are the raw text-box state — they
  // update on every keystroke. `debouncedFilters` is what the fetch
  // effect actually depends on; it lags the inputs by FILTER_DEBOUNCE_MS
  // so a user typing in the box doesn't fire a request per character.
  const [inputUserEmail, setInputUserEmail] = useState('')
  const [inputClientIp, setInputClientIp] = useState('')
  const [filterKind, setFilterKind] = useState<PageViewKind | ''>('')
  const [debouncedFilters, setDebouncedFilters] = useState<{
    user_email: string
    client_ip: string
    kind: PageViewKind | ''
  }>({ user_email: '', client_ip: '', kind: '' })
  const [includeBots, setIncludeBots] = useState(false)
  const [includeStaff, setIncludeStaff] = useState(false)
  const [actorRoleFilter, setActorRoleFilter] = useState<ActorRoleFilter>('')
  const [loading, setLoading] = useState(false)
  const [clearing, setClearing] = useState(false)
  const [clearModalOpen, setClearModalOpen] = useState(false)
  const [err, setErr] = useState('')
  // The destructive actions' (clear / purge / pseudonymize) own failure. Not
  // `err`: that banner belongs to the dashboard fetch, whose effect empties it
  // when it starts, and those actions refresh the dashboard after a failure as
  // after a success — so a refused pseudonymize or a failed ranged clear would
  // be shown for one frame and then wiped. This one stays until the next action
  // starts or the admin dismisses it.
  const [actionErr, setActionErr] = useState('')
  const [refreshTick, setRefreshTick] = useState(0)
  // Records when the last refresh fired so a rapid-click stream is
  // throttled to 1 click per REFRESH_COOLDOWN_MS even after a fast
  // request resolves and re-enables the button.
  const lastRefreshAt = useRef(0)
  // Tick that causes the cooldown timer to re-evaluate `disabled`
  // even when no other state has changed.
  const [, setCooldownTick] = useState(0)
  const cooldownTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(cooldownTimer.current), [])

  const onRefresh = () => {
    const now = Date.now()
    if (now - lastRefreshAt.current < REFRESH_COOLDOWN_MS) return
    lastRefreshAt.current = now
    setRefreshTick(x => x + 1)
    // Re-render once the cooldown elapses so `cooldownActive` flips
    // back to false and the button visibly re-enables. Kept in a ref so
    // unmounting (leaving the tab) cancels it instead of setting state on
    // an unmounted component.
    window.clearTimeout(cooldownTimer.current)
    cooldownTimer.current = window.setTimeout(() => setCooldownTick(x => x + 1), REFRESH_COOLDOWN_MS)
  }

  const onClearAll = () => {
    if (clearing) return
    setClearModalOpen(true)
  }

  // The destructive actions below each repeat their request until the server
  // reports it complete (lib/api deleteUntilComplete), committing as they go.
  // A failure part-way therefore leaves earlier batches applied, so the
  // dashboard is refreshed in `finally` — after a failure as after a success —
  // rather than going on showing rows that are already gone. It is skipped
  // only when nothing can have changed: no batch was acknowledged (`onBatch`)
  // and the failure was an outright refusal (mayHaveCommitted), e.g. a
  // pseudonymize answered 409 for want of a salt; a refresh then would only
  // spend a rate-limit token. Failures go to `actionErr`, which that refresh
  // leaves alone.
  const onClearSubmit = async (scope: AnalyticsClearScope) => {
    if (clearing) return
    setClearing(true)
    setActionErr('')
    let changed = false
    try {
      const r = await adminClearAnalytics({ ...scope, onBatch: () => { changed = true } })
      const totalDeleted = r.deleted_page_views + r.deleted_slide_dwell_events + r.deleted_tour_sessions
      setClearModalOpen(false)
      window.alert(t('analytics.clearAllDone', { deleted: fmt(totalDeleted, locale) }))
    } catch (e: any) {
      if (isAbortError(e)) return
      if (mayHaveCommitted(e)) changed = true
      setActionErr(actionErrorMessage(e, t, 'analytics.clearAllFailed'))
      setClearModalOpen(false)
    } finally {
      setClearing(false)
      if (changed) setRefreshTick(x => x + 1)
    }
  }

  // Erasure is matched in several stored forms at once (the bare name, its
  // ldap: / google: / basic: session subjects, the hashed digest), so the
  // confirm lists what the identifier actually reaches and how many rows go
  // with it — read from the server before anything is deleted. A preview that
  // finds nothing ends the flow without a delete request.
  const onPurgeUser = async () => {
    if (clearing) return
    const raw = window.prompt(t('analytics.purgeUserPrompt'), '')
    if (raw === null) return
    const identifier = raw.trim()
    if (!identifier) return
    setClearing(true)
    setActionErr('')
    // Only a delete that was sent can have changed anything; a preview that
    // failed or found nothing, or a declined confirm, leaves `changed` false.
    let changed = false
    try {
      let preview
      try {
        preview = await adminPreviewAnalyticsUser(identifier)
      } catch (e: any) {
        if (isAbortError(e)) return
        setActionErr(actionErrorMessage(e, t, 'analytics.purgeUserPreviewFailed', true))
        return
      }
      const total = preview.page_views + preview.slide_dwell_events + preview.tour_sessions
      if (total === 0) {
        window.alert(t('analytics.purgeUserNoMatch', { identifier }))
        return
      }
      const subjectLines = preview.subjects.map(s => `  ${s}`)
      if (preview.subjects.length >= ANALYTICS_PREVIEW_SUBJECT_CAP) {
        subjectLines.push(`  ${t('analytics.purgeUserSubjectsCapped', { n: ANALYTICS_PREVIEW_SUBJECT_CAP })}`)
      }
      const ok = window.confirm(t('analytics.purgeUserConfirm', {
        identifier,
        subjects: subjectLines.join('\n') || '  —',
        pageViews: fmt(preview.page_views, locale),
        dwell: fmt(preview.slide_dwell_events, locale),
        tours: fmt(preview.tour_sessions, locale),
        total: fmt(total, locale),
      }))
      if (!ok) return
      const r = await adminClearAnalyticsUser(identifier, { onBatch: () => { changed = true } })
      const totalDeleted = r.deleted_page_views + r.deleted_slide_dwell_events + r.deleted_tour_sessions
      window.alert(t('analytics.purgeUserDone', { deleted: fmt(totalDeleted, locale) }))
    } catch (e: any) {
      if (isAbortError(e)) return
      // Only the delete itself throws here (the preview has its own catch).
      if (mayHaveCommitted(e)) changed = true
      setActionErr(actionErrorMessage(e, t, 'analytics.clearAllFailed'))
    } finally {
      setClearing(false)
      if (changed) setRefreshTick(x => x + 1)
    }
  }

  // Rewrites the rows recorded in plain mode into the hashed form (the
  // contract of a switch to Pseudonymous mode, applied retroactively). Like
  // the deletes it repeats until complete and commits per batch, so the
  // refresh runs whatever the outcome — except after a refusal before any
  // batch (409 analyticsSaltUnavailable), which changed nothing.
  const onPseudonymize = async () => {
    if (clearing) return
    if (!window.confirm(t('analytics.pseudonymizeConfirm'))) return
    setClearing(true)
    setActionErr('')
    let changed = false
    try {
      const r = await adminPseudonymizeAnalytics({ onBatch: () => { changed = true } })
      const updated = fmt(
        r.updated.page_views + r.updated.slide_dwell_events + r.updated.tour_sessions,
        locale,
      )
      window.alert(t(r.complete ? 'analytics.pseudonymizeDone' : 'analytics.pseudonymizePartial', { updated }))
    } catch (e: any) {
      if (isAbortError(e)) return
      if (mayHaveCommitted(e)) changed = true
      setActionErr(actionErrorMessage(e, t, 'analytics.pseudonymizeFailed', true))
    } finally {
      setClearing(false)
      if (changed) setRefreshTick(x => x + 1)
    }
  }

  const cooldownActive = Date.now() - lastRefreshAt.current < REFRESH_COOLDOWN_MS

  // Push the text inputs into `debouncedFilters` after the user pauses typing.
  // Preserve `kind` (owned by the immediate effect below) so the two don't
  // clobber each other.
  useEffect(() => {
    const handle = setTimeout(() => {
      setDebouncedFilters(prev =>
        prev.user_email === inputUserEmail && prev.client_ip === inputClientIp
          ? prev
          : { ...prev, user_email: inputUserEmail, client_ip: inputClientIp }
      )
    }, FILTER_DEBOUNCE_MS)
    return () => clearTimeout(handle)
  }, [inputUserEmail, inputClientIp])

  // `filterKind` is a discrete `<select>`, so apply it immediately rather than
  // making the user wait out the text-input debounce.
  useEffect(() => {
    setDebouncedFilters(prev => (prev.kind === filterKind ? prev : { ...prev, kind: filterKind }))
  }, [filterKind])

  const clearFilters = () => {
    setInputUserEmail('')
    setInputClientIp('')
    setFilterKind('')
    setActorRoleFilter('')
    setDebouncedFilters({ user_email: '', client_ip: '', kind: '' })
  }

  const onSelectPreset = (n: PresetDays) => {
    setCustomError('')
    setCustomOpen(false)
    setRange({ mode: 'preset', days: n })
  }

  // Only opens the date inputs; nothing is fetched until Apply (the window
  // shown stays the preset's meanwhile, and its button stays highlighted).
  const onSwitchToCustom = () => {
    if (customOpen) return
    setCustomError('')
    setCustomOpen(true)
    // Seed the date inputs from the active preset so the user starts from
    // "the same span I just had", not Jan 1 1970. A preset of N days is the
    // N calendar days ending today (the server aligns it to midnight of
    // today-(N-1) in the analytics zone), so the inclusive custom twin starts
    // N-1 days back; the dates are read in that zone, so "today" is today
    // there, not in the browser.
    if (range.mode === 'preset') {
      setCustomStart(todayInServerTz(serverTz, range.days - 1))
      setCustomEnd(todayInServerTz(serverTz))
    }
  }

  const onApplyCustomRange = () => {
    if (!customStart || !customEnd) {
      setCustomError(t('analytics.customRangeMissing'))
      return
    }
    if (customStart > customEnd) {
      setCustomError(t('analytics.customRangeInverted'))
      return
    }
    // Mirror the backend's `_MAX_WINDOW_DAYS = 365` cap on the client so
    // the user gets an inline error instead of round-tripping to a 400.
    // Both endpoints are inclusive → span = (end − start) days + 1.
    const startMs = Date.parse(`${customStart}T00:00:00Z`)
    const endMs = Date.parse(`${customEnd}T00:00:00Z`)
    if (Number.isFinite(startMs) && Number.isFinite(endMs)) {
      const span = Math.floor((endMs - startMs) / 86_400_000) + 1
      if (span > MAX_WINDOW_DAYS) {
        setCustomError(t('analytics.customRangeTooLarge', { max: MAX_WINDOW_DAYS }))
        return
      }
    }
    setCustomError('')
    setRange({ mode: 'custom', start: customStart, end: customEnd })
  }

  const onDownloadCsv = async () => {
    if (csvBusy) return
    const dataset = csvDataset
    setCsvBusy(dataset)
    setCsvErr('')
    try {
      // Recent-log filters only apply when exporting the raw `recent`
      // dataset; the pre-aggregated tables never expose per-row filters
      // so forwarding them would be silently ignored.
      const recentFilters = dataset === 'recent'
        ? {
            user_email: debouncedFilters.user_email || undefined,
            client_ip: debouncedFilters.client_ip || undefined,
            kind: debouncedFilters.kind || undefined,
          }
        : {}
      await adminDownloadAnalyticsCsv(dataset, range, {
        ...recentFilters,
        include_bots: includeBots,
        include_staff: includeStaff,
        actor_role: dataset === 'recent' ? (actorRoleFilter || undefined) : undefined,
        // Generous cap so the CSV isn't silently truncated to the
        // on-screen 200-row preview.
        limit: 10000,
      })
    } catch (e: any) {
      if (isAbortError(e)) return
      if (isRateLimitError(e)) {
        setCsvErr(t('common.rateLimited'))
        return
      }
      setCsvErr(apiErrorMessage(e, t) || t('analytics.csvFailed'))
    } finally {
      setCsvBusy(null)
    }
  }

  // Dashboard (summary + 5 rankings) and the recent-activity log are
  // fetched separately so a filter change on the log doesn't redo the
  // expensive aggregate queries. The initial render spends 4 nginx
  // rate-limit tokens — the consolidated dashboard call plus tour-completion,
  // slide-dwell, and the recent log — rather than one request per ranking.
  //
  // `rangeKey` collapses the AnalyticsRange object to a primitive so the
  // effect dep array compares by value rather than reference identity
  // (object literals re-create on every render).
  const rangeKey = range.mode === 'preset'
    ? `preset:${range.days}`
    : `custom:${range.start}:${range.end}`
  useEffect(() => {
    const ctrl = new AbortController()
    setLoading(true)
    setErr('')
    // Tour completion + slide dwell are independent endpoints (separate
    // tables) so they fetch in parallel with the dashboard. A failure of one
    // of them alone is swallowed: that panel falls back to its empty state
    // rather than raising a banner that would shadow the rest of the
    // dashboard. (Aborts are re-thrown so a superseded run applies nothing.)
    const sidePanel = <T,>(p: Promise<T[]>): Promise<T[]> =>
      p.catch(e => { if (isAbortError(e)) throw e; return [] })
    // Every block is applied together, once all three have settled, so the
    // screen never mixes windows: a failed dashboard request does not leave
    // the previous window's summary and rankings beside side panels already
    // showing the new one. On failure the whole block is cleared under the
    // error banner instead of going on showing numbers for a window that is
    // no longer selected.
    Promise.all([
      adminGetAnalyticsDashboard(range, {
        limit: 20,
        include_bots: includeBots,
        include_staff: includeStaff,
        signal: ctrl.signal,
      }),
      sidePanel(adminGetTourCompletion(range, { include_staff: includeStaff, signal: ctrl.signal })),
      sidePanel(adminGetSlideDwell(range, { include_staff: includeStaff, signal: ctrl.signal })),
    ])
      .then(([d, tc, sd]) => {
        if (ctrl.signal.aborted) return
        setSummary(d.summary)
        setTopSlides(d.top_slides)
        setTopTours(d.top_tours)
        setReferrers(d.referrers)
        setUa(d.ua_breakdown)
        setTopUsers(d.top_users)
        setServerTz(d.server_tz)
        setTourCompletion(tc)
        setSlideDwell(sd)
      })
      .catch(e => {
        if (isAbortError(e) || ctrl.signal.aborted) return
        // serverTz is kept: it describes the server, not the window.
        setSummary(null)
        setTopSlides([])
        setTopTours([])
        setReferrers([])
        setUa([])
        setTopUsers([])
        setTourCompletion([])
        setSlideDwell([])
        if (isRateLimitError(e)) {
          setErr(t('common.rateLimited'))
          return
        }
        setErr(apiErrorMessage(e, t) || t('analytics.loadFailed'))
      })
      .finally(() => {
        // Only the live request may clear the spinner. A superseded request
        // (aborted because the range / filters changed) settles AFTER the new
        // run has already set loading=true; without this guard its finally
        // would flip loading back to false mid-flight, re-enabling the
        // Refresh / Purge / Clear buttons while a fetch is still in flight.
        if (!ctrl.signal.aborted) setLoading(false)
      })
    return () => ctrl.abort()
    // `t` is intentionally NOT in deps: it changes identity on locale switch,
    // and re-running this effect would refetch the dashboard (consuming a
    // rate-limit token) for a UI-only change. Error strings stored in `err`
    // stay in their original locale until the next refresh — acceptable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rangeKey, refreshTick, includeBots, includeStaff])

  useEffect(() => {
    const ctrl = new AbortController()
    // Push the same date range and bot/admin filters into the Recent log
    // so the table reflects what the dashboard above is showing. Without
    // this, picking "Last 7 days" would leave the Recent log on its
    // global last-200 rows, confusing users when the summary says 0 hits but
    // older rows still fill the log.
    const recentParams = range.mode === 'custom'
      ? { start_date: range.start, end_date: range.end }
      : { days: range.days }
    setRecentLoading(true)
    adminGetRecentPageViews(
      {
        limit: RECENT_LIMIT,
        ...recentParams,
        user_email: debouncedFilters.user_email || undefined,
        client_ip: debouncedFilters.client_ip || undefined,
        kind: debouncedFilters.kind || undefined,
        include_bots: includeBots,
        include_staff: includeStaff,
        actor_role: actorRoleFilter || undefined,
      },
      ctrl.signal,
    )
      .then(rows => {
        setRecent(rows)
        setRecentErr('')
      })
      .catch(e => {
        if (isAbortError(e)) return
        // The rows on screen answered the previous filters; left in place
        // under a failed request they would read as the new filters' result.
        // Cleared, with the reason beside the log — a rate limit included,
        // which dropped silently reads as "the filter matched nothing".
        setRecent([])
        setRecentErr(isRateLimitError(e)
          ? t('common.rateLimited')
          : (apiErrorMessage(e, t) || t('analytics.loadFailed')))
      })
      .finally(() => {
        // Same superseded-request guard as the dashboard spinner.
        if (!ctrl.signal.aborted) setRecentLoading(false)
      })
    return () => ctrl.abort()
    // `t` deliberately omitted (see dashboard effect above for rationale).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    rangeKey,
    refreshTick,
    includeBots,
    includeStaff,
    actorRoleFilter,
    debouncedFilters.user_email,
    debouncedFilters.client_ip,
    debouncedFilters.kind,
  ])

  const totals = summary?.totals
  const grand = totals ? totals.top + totals.viewer + totals.tour : 0

  // The dwell / tour beacons drop bot traffic before it is stored (the
  // tables keep no UA class to filter on later), so "Include bots" cannot
  // change these two panels; say so while it is ticked rather than let the
  // unchanged numbers read as a bug.
  const botsHint = includeBots
    ? <div style={{ fontSize: 11, opacity: 0.7, marginBottom: 4 }}>{t('analytics.botsNotApplicable')}</div>
    : null

  // The log is pending from a keystroke in a filter box (the request waits out
  // the debounce) until its response lands, so the rows on screen are marked
  // as not yet answering what the filters now say.
  const recentPending = recentLoading
    || inputUserEmail !== debouncedFilters.user_email
    || inputClientIp !== debouncedFilters.client_ip

  const rankingsBlock = (
    <>
      <SubHeading>{t('analytics.topSlides')}</SubHeading>
      {topSlides.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>{t('analytics.empty')}</div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ ...TH, width: 32 }}>{t('analytics.rank')}</th>
              <th style={TH}>{t('analytics.title2')}</th>
              <th style={{ ...TH, width: 80, textAlign: 'right' }}>{t('analytics.views')}</th>
              <th style={{ ...TH, width: 80, textAlign: 'right' }}>{t('analytics.uniques')}</th>
            </tr>
          </thead>
          <tbody>
            {topSlides.map((row, i) => (
              <tr key={row.slide_id}>
                <td style={TD}>{i + 1}</td>
                <td style={TD}>
                  <MaybeLink href={slideHref(row.public_id)}>
                    {row.title || `#${row.slide_id}`}
                  </MaybeLink>
                  {row.catalog_id && <span style={{ marginLeft: 6, fontSize: 10, opacity: 0.6 }}>{row.catalog_id}</span>}
                </td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{fmt(row.views, locale)}</td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{fmt(row.unique_ips, locale)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <SubHeading>{t('analytics.topTours')}</SubHeading>
      {topTours.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>{t('analytics.empty')}</div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ ...TH, width: 32 }}>{t('analytics.rank')}</th>
              <th style={TH}>{t('analytics.title2')}</th>
              <th style={{ ...TH, width: 80, textAlign: 'right' }}>{t('analytics.views')}</th>
              <th style={{ ...TH, width: 80, textAlign: 'right' }}>{t('analytics.uniques')}</th>
            </tr>
          </thead>
          <tbody>
            {topTours.map((row, i) => (
              <tr key={row.tour_id}>
                <td style={TD}>{i + 1}</td>
                <td style={TD}>
                  <MaybeLink href={tourHref(row.slide_public_id, row.public_id)}>
                    {row.tour_title || `#${row.tour_id}`}
                  </MaybeLink>
                  <div style={{ fontSize: 10, opacity: 0.6 }}>
                    {row.slide_title || `slide #${row.slide_id}`}
                  </div>
                </td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{fmt(row.views, locale)}</td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{fmt(row.unique_ips, locale)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <SubHeading>{t('analytics.topUsers')}</SubHeading>
      {topUsers.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>{t('analytics.empty')}</div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ ...TH, width: 32 }}>{t('analytics.rank')}</th>
              <th style={TH}>{t('analytics.user')}</th>
              <th style={{ ...TH, width: 70, textAlign: 'right' }}>{t('analytics.views')}</th>
              <th style={{ ...TH, width: 70, textAlign: 'right' }}>{t('analytics.uniqueDays')}</th>
              <th style={{ ...TH, width: 70, textAlign: 'right' }}>{t('analytics.uniques')}</th>
            </tr>
          </thead>
          <tbody>
            {/* The server leaves anonymous traffic out of this ranking, so
                every row carries an identifier. */}
            {topUsers.map((row, i) => (
              <tr key={row.user_email}>
                <td style={TD}>{i + 1}</td>
                <td style={TD}>
                  <button
                    onClick={() => setInputUserEmail(row.user_email)}
                    style={{ background: 'none', border: 'none', padding: 0, color: 'var(--color-link, inherit)', cursor: 'pointer', textDecoration: 'underline' }}
                    title={row.user_email}
                  >
                    {shortDigest(row.user_email)}
                  </button>
                </td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{fmt(row.views, locale)}</td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{fmt(row.unique_days, locale)}</td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{fmt(row.unique_ips, locale)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <SubHeading>{t('analytics.referrers')}</SubHeading>
      {referrers.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>{t('analytics.empty')}</div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={TH}>{t('analytics.referrers')}</th>
              <th style={{ ...TH, width: 80, textAlign: 'right' }}>{t('analytics.views')}</th>
            </tr>
          </thead>
          <tbody>
            {referrers.map((row, i) => (
              <tr key={`${row.referrer_host}|${i}`}>
                <td style={TD}>{row.referrer_host || <span style={{ opacity: 0.6 }}>{t('analytics.directTraffic')}</span>}</td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{fmt(row.views, locale)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <SubHeading>{t('analytics.uaBreakdown')}</SubHeading>
      {ua.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>{t('analytics.empty')}</div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={TH}>{t('analytics.deviceType')}</th>
              <th style={{ ...TH, width: 80, textAlign: 'right' }}>{t('analytics.views')}</th>
            </tr>
          </thead>
          <tbody>
            {ua.map(row => (
              <tr key={row.ua_class}>
                <td style={TD}>{row.ua_class}</td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{fmt(row.views, locale)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <SubHeading>{t('analytics.tourCompletion')}</SubHeading>
      {botsHint}
      {tourCompletion.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>{t('analytics.empty')}</div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={TH}>{t('analytics.tour')}</th>
              <th style={{ ...TH, width: 70, textAlign: 'right' }}>{t('analytics.sessions')}</th>
              <th style={{ ...TH, width: 80, textAlign: 'right' }}>{t('analytics.completion')}</th>
              <th style={{ ...TH, width: 90, textAlign: 'right' }}>{t('analytics.avgCompletion')}</th>
            </tr>
          </thead>
          <tbody>
            {tourCompletion.map((row) => (
              <tr key={row.tour_id}>
                <td style={TD}>
                  <MaybeLink href={tourHref(row.slide_public_id, row.public_id)}>
                    {row.tour_title || `#${row.tour_id}`}
                  </MaybeLink>
                  <div style={{ fontSize: 10, opacity: 0.6 }}>
                    {row.slide_title || `slide #${row.slide_id}`}
                  </div>
                </td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>
                  {fmt(row.sessions_completed, locale)} / {fmt(row.sessions_total, locale)}
                </td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>
                  {fmtPercent(row.completion_rate, locale)}
                </td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>
                  {row.sessions_completed > 0 ? formatDurationMs(row.avg_completion_ms) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <SubHeading>{t('analytics.slideDwell')}</SubHeading>
      {botsHint}
      {slideDwell.length === 0 ? (
        <div style={{ fontSize: 12, opacity: 0.7 }}>{t('analytics.empty')}</div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={TH}>{t('analytics.title2')}</th>
              <th style={{ ...TH, width: 70, textAlign: 'right' }}>{t('analytics.sessions')}</th>
              <th style={{ ...TH, width: 90, textAlign: 'right' }}>{t('analytics.avgDwell')}</th>
              <th style={{ ...TH, width: 70, textAlign: 'right' }}>{t('analytics.uniqueUsers')}</th>
            </tr>
          </thead>
          <tbody>
            {slideDwell.map((row) => (
              <tr key={row.slide_id}>
                <td style={TD}>
                  <MaybeLink href={slideHref(row.public_id)}>
                    {row.title || `#${row.slide_id}`}
                  </MaybeLink>
                  {row.catalog_id && <span style={{ marginLeft: 6, fontSize: 10, opacity: 0.6 }}>{row.catalog_id}</span>}
                </td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{fmt(row.sessions, locale)}</td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{formatDurationMs(row.avg_dwell_ms)}</td>
                <td style={{ ...TD, textAlign: 'right', fontFamily: 'monospace' }}>{fmt(row.unique_users, locale)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )

  const recentBlock = (
    <>
      <SubHeading>
        {t('analytics.recentLog')}
        {/* Always mounted, so a screen reader announces the text when it
            appears (a live region inserted with its content often is not). */}
        <span aria-live="polite" style={{ marginLeft: 6, fontWeight: 400, textTransform: 'none', letterSpacing: 0, opacity: 0.7 }}>
          {recentPending ? t('analytics.loading') : ''}
        </span>
      </SubHeading>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 4 }}>
        <input
          type="text"
          placeholder={t('analytics.filterUser')}
          value={inputUserEmail}
          onChange={e => setInputUserEmail(e.target.value)}
          style={{ fontSize: 12, padding: '3px 6px', minWidth: 160 }}
        />
        <input
          type="text"
          placeholder={t('analytics.filterIp')}
          value={inputClientIp}
          onChange={e => setInputClientIp(e.target.value)}
          style={{ fontSize: 12, padding: '3px 6px', minWidth: 130 }}
        />
        <select
          value={filterKind}
          onChange={e => setFilterKind((e.target.value || '') as PageViewKind | '')}
          style={{ fontSize: 12, padding: '3px 6px' }}
        >
          <option value="">{t('analytics.allKinds')}</option>
          <option value="top">{t('analytics.kindTop')}</option>
          <option value="viewer">{t('analytics.kindViewer')}</option>
          <option value="tour">{t('analytics.kindTour')}</option>
        </select>
        <select
          value={actorRoleFilter}
          onChange={e => setActorRoleFilter((e.target.value || '') as ActorRoleFilter)}
          style={{ fontSize: 12, padding: '3px 6px' }}
          title={t('analytics.actorRoleFilterTitle')}
        >
          <option value="">{t('analytics.actorRoleAny')}</option>
          <option value="none">{t('analytics.actorRoleLearner')}</option>
          <option value="admin">{t('roles.admin')}</option>
          <option value="manager">{t('roles.manager')}</option>
          <option value="instructor">{t('roles.instructor')}</option>
        </select>
        {(inputUserEmail || inputClientIp || filterKind || actorRoleFilter) && (
          <button
            onClick={clearFilters}
            style={{ fontSize: 12, padding: '3px 8px' }}
          >
            {t('analytics.clearFilters')}
          </button>
        )}
      </div>
      {recentErr && (
        <div role="alert" style={{ fontSize: 12, color: 'var(--color-danger-fg)', marginBottom: 4 }}>{recentErr}</div>
      )}
      {recent.length === 0 ? (
        !recentErr && <div style={{ fontSize: 12, opacity: 0.7 }}>{t('analytics.empty')}</div>
      ) : (
        <div
          className="overscroll-panel"
          aria-busy={recentPending || undefined}
          style={{ maxHeight: 'calc(100vh - 240px)', overflow: 'auto', border: '1px solid var(--color-border)', borderRadius: 4, opacity: recentPending ? 0.55 : 1 }}
        >
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead style={{ position: 'sticky', top: 0, background: 'var(--color-surface)' }}>
              <tr>
                <th style={{ ...TH, width: 180 }}>
                  {t('analytics.time')}
                  {serverTz && (
                    <span style={{ fontWeight: 400, opacity: 0.7, marginLeft: 4 }}>
                      ({serverTzLabel(serverTz)})
                    </span>
                  )}
                </th>
                <th style={{ ...TH, width: 60 }}>{t('analytics.kind')}</th>
                <th style={{ ...TH, width: 80 }}>{t('analytics.role')}</th>
                {/* A floor under the page column: auto table layout squeezes it to
                    min-content, and Japanese labels have no spaces to wrap at, so
                    "トップページ" would stack one character per line. In px, not em:
                    an em here is the header's own 10px font, so '6em' would give 60px,
                    short of six full-width characters in the 12px cells below
                    (72px). 96px covers them with room for a fallback font's
                    wider glyphs; the cells' 6px side padding comes on top, as
                    it does on this header's. */}
                <th style={{ ...TH, minWidth: PAGE_COL_MIN_PX }}>{t('analytics.page')}</th>
                <th style={{ ...TH, width: 140 }}>{t('analytics.referrer')}</th>
                <th style={{ ...TH, width: 160 }}>{t('analytics.user')}</th>
                <th style={{ ...TH, width: 110 }}>{t('analytics.ip')}</th>
                <th style={{ ...TH, width: 70 }}>{t('analytics.deviceType')}</th>
              </tr>
            </thead>
            <tbody>
              {recent.map(row => {
                const timeStr = row.occurred_at ? formatInServerTz(row.occurred_at, serverTz, locale) : ''
                let pageLabel: React.ReactNode = '/'
                let pageHref: string | null = '/'
                if (row.kind === 'viewer' && row.slide_id) {
                  pageLabel = row.slide_title || `slide #${row.slide_id}`
                  pageHref = slideHref(row.slide_public_id)
                } else if (row.kind === 'tour' && row.slide_id && row.tour_id) {
                  pageLabel = `${row.tour_title || `tour #${row.tour_id}`} — ${row.slide_title || `slide #${row.slide_id}`}`
                  pageHref = tourHref(row.slide_public_id, row.tour_public_id)
                } else if (row.kind === 'top') {
                  pageLabel = t('analytics.kindTop')
                  pageHref = '/'
                }
                return (
                  <tr key={row.id}>
                    <td style={{ ...TD, fontFamily: 'monospace', fontSize: 11 }}>{timeStr}</td>
                    <td style={TD}>{row.kind}</td>
                    <td style={TD}>
                      {row.actor_role && <ActorRoleBadge role={row.actor_role} />}
                    </td>
                    <td style={TD}>
                      {pageHref ? (
                        <a href={pageHref} target="_blank" rel="noopener noreferrer">{pageLabel}</a>
                      ) : pageLabel}
                    </td>
                    <td style={TD} title={row.referrer_host || undefined}>
                      {row.referrer_host
                        ? <span style={{ wordBreak: 'break-all' }}>{row.referrer_host}</span>
                        : <span style={{ opacity: 0.6 }}>{t('analytics.directTraffic')}</span>}
                    </td>
                    <td style={TD}>
                      {row.user_email ? (
                        <button
                          onClick={() => setInputUserEmail(row.user_email)}
                          style={{ background: 'none', border: 'none', padding: 0, color: 'var(--color-link, inherit)', cursor: 'pointer', textDecoration: 'underline' }}
                          title={row.user_email}
                        >
                          {shortDigest(row.user_email)}
                        </button>
                      ) : (
                        <span style={{ opacity: 0.6 }}>{t('analytics.anonymous')}</span>
                      )}
                    </td>
                    <td style={{ ...TD, fontFamily: 'monospace', fontSize: 11 }}>
                      <button
                        onClick={() => setInputClientIp(row.client_ip)}
                        style={{ background: 'none', border: 'none', padding: 0, color: 'inherit', cursor: 'pointer' }}
                        title={row.client_ip}
                      >
                        {shortDigest(row.client_ip)}
                      </button>
                    </td>
                    <td style={{ ...TD, cursor: row.user_agent ? 'help' : 'default' }} title={row.user_agent || undefined}>{row.ua_class}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {recent.length >= RECENT_LIMIT && (
        <div style={{ fontSize: 11, opacity: 0.7, marginTop: 4 }}>
          {t('analytics.recentCapped', { n: fmt(RECENT_LIMIT, locale) })}
        </div>
      )}
    </>
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12, opacity: 0.7 }}>{t('analytics.range')}</span>
          {PRESET_DAYS.map(n => {
            const active = range.mode === 'preset' && range.days === n
            return (
              <button
                key={n}
                onClick={() => onSelectPreset(n)}
                style={{
                  padding: '4px 10px',
                  fontSize: 12,
                  background: active ? 'var(--color-brand-600)' : 'transparent',
                  color: active ? 'white' : 'inherit',
                  border: '1px solid var(--color-border)',
                  borderRadius: 3,
                  cursor: 'pointer',
                }}
              >
                {t(PRESET_LABEL_KEY[n])}
              </button>
            )
          })}
          <button
            onClick={onSwitchToCustom}
            aria-expanded={customOpen}
            style={{
              padding: '4px 10px',
              fontSize: 12,
              background: range.mode === 'custom' ? 'var(--color-brand-600)' : 'transparent',
              color: range.mode === 'custom' ? 'white' : 'inherit',
              // Open but not yet applied: marked as being edited, while the
              // preset whose data is still on screen keeps the fill.
              border: customOpen && range.mode !== 'custom'
                ? '1px solid var(--color-brand-600)'
                : '1px solid var(--color-border)',
              borderRadius: 3,
              cursor: 'pointer',
            }}
            title={t('analytics.rangeCustomHint')}
          >
            {t('analytics.rangeCustom')}
          </button>
          {customOpen && (
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <input
                type="date"
                value={customStart}
                max={customEnd || undefined}
                onChange={e => setCustomStart(e.target.value)}
                style={{ fontSize: 12, padding: '3px 6px' }}
                aria-label={t('analytics.startDate')}
              />
              <span style={{ fontSize: 12, opacity: 0.7 }}>—</span>
              <input
                type="date"
                value={customEnd}
                min={customStart || undefined}
                max={todayInServerTz(serverTz)}
                onChange={e => setCustomEnd(e.target.value)}
                style={{ fontSize: 12, padding: '3px 6px' }}
                aria-label={t('analytics.endDate')}
              />
              <button
                onClick={onApplyCustomRange}
                disabled={
                  loading
                  || !customStart
                  || !customEnd
                  || (range.mode === 'custom' && range.start === customStart && range.end === customEnd)
                }
                style={{ fontSize: 12, padding: '3px 8px' }}
              >
                {t('analytics.apply')}
              </button>
            </span>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
            <input
              type="checkbox"
              checked={includeBots}
              onChange={e => setIncludeBots(e.target.checked)}
            />
            {t('analytics.includeBots')}
          </label>
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
            <input
              type="checkbox"
              checked={includeStaff}
              onChange={e => setIncludeStaff(e.target.checked)}
            />
            {t('analytics.includeStaff')}
          </label>
          <select
            value={csvDataset}
            onChange={e => setCsvDataset(e.target.value as CsvDataset)}
            disabled={csvBusy !== null}
            style={{ fontSize: 12, padding: '3px 6px' }}
            aria-label={t('analytics.csvDatasetLabel')}
            title={t('analytics.csvDatasetLabel')}
          >
            <option value="recent">{t('analytics.csvDatasetRecent')}</option>
            <option value="summary">{t('analytics.csvDatasetSummary')}</option>
            <option value="daily">{t('analytics.csvDatasetDaily')}</option>
            <option value="top-slides">{t('analytics.csvDatasetTopSlides')}</option>
            <option value="top-tours">{t('analytics.csvDatasetTopTours')}</option>
            <option value="top-users">{t('analytics.csvDatasetTopUsers')}</option>
            <option value="referrers">{t('analytics.csvDatasetReferrers')}</option>
            <option value="ua">{t('analytics.csvDatasetUa')}</option>
          </select>
          <button
            onClick={onDownloadCsv}
            disabled={csvBusy !== null}
            style={{ fontSize: 12, padding: '4px 10px' }}
            title={t('analytics.csvDownloadHint')}
          >
            {csvBusy ? t('analytics.loading') : t('analytics.csvDownload')}
          </button>
          {csvErr && (
            <span role="alert" style={{ fontSize: 12, color: 'var(--color-danger-fg)' }}>{csvErr}</span>
          )}
          <button
            onClick={onRefresh}
            disabled={loading || cooldownActive}
            style={{ fontSize: 12 }}
            title={cooldownActive && !loading ? t('common.rateLimited') : undefined}
          >
            {loading ? t('analytics.loading') : t('analytics.refresh')}
          </button>
          {/* The destructive actions (purge, pseudonymize, clear) are
              admin-only server-side (require_admin); a manager / instructor
              granted analytics view would only 403. */}
          {isAdminFromToken() && (<>
          <button
            onClick={onPurgeUser}
            disabled={clearing || loading}
            style={{
              fontSize: 12,
              color: 'var(--color-danger-fg)',
              borderColor: 'var(--color-danger-fg)',
            }}
            title={t('analytics.purgeUserHelp')}
          >
            {t('analytics.purgeUser')}
          </button>
          <button
            onClick={onPseudonymize}
            disabled={clearing || loading}
            style={{
              fontSize: 12,
              color: 'var(--color-danger-fg)',
              borderColor: 'var(--color-danger-fg)',
            }}
            title={t('analytics.pseudonymizeHelp')}
          >
            {t('analytics.pseudonymize')}
          </button>
          <button
            onClick={onClearAll}
            disabled={clearing || loading}
            style={{
              fontSize: 12,
              color: 'var(--color-danger-fg)',
              borderColor: 'var(--color-danger-fg)',
            }}
          >
            {clearing ? t('analytics.loading') : t('analytics.clearAll')}
          </button>
          </>)}
        </div>
      </div>
      {customError && <div style={{ fontSize: 12, color: 'var(--color-danger-fg)' }}>{customError}</div>}

      {actionErr && (
        <div role="alert" style={{ fontSize: 13, color: 'var(--color-danger-fg)', display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span>{actionErr}</span>
          <button type="button" onClick={() => setActionErr('')} style={{ fontSize: 12 }}>{t('adminDash.dismiss')}</button>
        </div>
      )}
      {err && <div role="alert" style={{ fontSize: 13, color: 'var(--color-danger-fg)' }}>{err}</div>}

      {summary && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <SubHeading>{t('analytics.summary')}</SubHeading>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
            <SummaryCard label={t('analytics.totalViews')} value={fmt(grand, locale)} />
            <SummaryCard label={t('analytics.uniqueIps')} value={fmt(summary.unique_ips, locale)} />
            <SummaryCard label={t('analytics.uniqueUsers')} value={fmt(summary.unique_users, locale)} />
            <SummaryCard label={t('analytics.kindTop')} value={fmt(totals?.top ?? 0, locale)} />
            <SummaryCard label={t('analytics.kindViewer')} value={fmt(totals?.viewer ?? 0, locale)} />
            <SummaryCard label={t('analytics.kindTour')} value={fmt(totals?.tour ?? 0, locale)} />
          </div>
          <SubHeading>
            {t('analytics.dailyChart')}
            <span style={{ marginLeft: 6, fontWeight: 400, textTransform: 'none', letterSpacing: 0, opacity: 0.7 }}>
              ({t('analytics.dailyChartTzNote', { tz: serverTzLabel(serverTz) || 'UTC' })})
            </span>
          </SubHeading>
          {(() => {
            const trimmed = trimLeadingZeroDays(summary.daily)
            return trimmed.length === 0 ? (
              <div style={{ fontSize: 12, opacity: 0.7 }}>{t('analytics.empty')}</div>
            ) : (
              <DailyChart daily={trimmed} locale={locale} />
            )
          })()}
        </div>
      )}

      <div className="analytics-grid" style={{ display: 'grid', gap: 24, gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1.4fr)' }}>
        <section>{rankingsBlock}</section>
        <section>{recentBlock}</section>
      </div>

      <style>{`
        @media (max-width: 960px) {
          .analytics-grid {
            grid-template-columns: minmax(0, 1fr) !important;
          }
        }
      `}</style>

      {clearModalOpen && (
        <AdminAnalyticsClearModal
          onCancel={() => { if (!clearing) setClearModalOpen(false) }}
          onSubmit={onClearSubmit}
          tzLabel={serverTzLabel(serverTz)}
        />
      )}
    </div>
  )
}
