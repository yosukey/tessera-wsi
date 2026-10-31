import React, { useEffect, useRef, useState } from 'react'
import {
  SeasonalBackground,
  adminDeleteSeasonalBackgroundImage,
  adminListSeasonalBackgrounds,
  adminResetSeasonalBackground,
  adminUpdateSeasonalBackground,
  adminUploadSeasonalBackgroundImage,
  isAbortError,
} from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { failureMessage } from '../../lib/admin/apiError'
import { UPLOAD_LIMITS, isValidHexColor } from '../../lib/admin/validation'
import { cssImageUrl, useAdminBlobUrl } from '../../hooks/useAdminBlobUrl'
import { useInFlightKey } from '../../hooks/useInFlight'
import { LockFieldset } from '../ui'
import { SectionMessage, itemRow, subHeading, useSectionMessage } from './settingsSectionUi'

const SLOTS = [1, 2, 3, 4, 5] as const

const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

function clampDay(month: number, day: number): number {
  const max = DAYS_IN_MONTH[Math.max(0, Math.min(11, month - 1))]
  if (day < 1) return 1
  if (day > max) return max
  return day
}

function emptySlot(slot: number): SeasonalBackground {
  return {
    slot,
    enabled: false,
    label: '',
    start_month: 1,
    start_day: 1,
    end_month: 1,
    end_day: 1,
    image_url: '',
    has_image: false,
    title_color: '',
  }
}

// A slot's tiled image. `url` is the admin image route (it serves a slot
// that is not enabled yet, which the public route 404s), fetched with the
// admin bearer.
function SlotPreview({ url }: { url: string }) {
  const { t } = useI18n()
  const preview = useAdminBlobUrl(url)
  return (
    <div
      aria-label={t('common.previewAlt')}
      style={{
        width: 64,
        height: 32,
        backgroundImage: cssImageUrl(preview),
        backgroundRepeat: 'repeat',
        border: '1px solid var(--color-border)',
        borderRadius: 3,
      }}
    />
  )
}

function dateOrdinal(month: number, day: number): number {
  return month * 100 + day
}

function isRangeInvalid(sb: SeasonalBackground): boolean {
  // Only police the range while the slot is enabled. A disabled slot
  // doesn't display on the top page, so red-bordering it (and blocking
  // its Save button) just because a previously edited window happens to
  // wrap years is noise — the admin wanting to "turn this off" can't.
  // Note: the server still validates dates on every PATCH, so saving
  // disabled+invalid will surface a 422 the admin must resolve before
  // re-enabling. This only stops the range check blocking the disable flow.
  if (!sb.enabled) return false
  return dateOrdinal(sb.start_month, sb.start_day) > dateOrdinal(sb.end_month, sb.end_day)
}

type RawDates = { sm: string; sd: string; em: string; ed: string }

function rawFromSb(sb: SeasonalBackground): RawDates {
  return {
    sm: String(sb.start_month),
    sd: String(sb.start_day),
    em: String(sb.end_month),
    ed: String(sb.end_day),
  }
}

// Parse user-typed text into a clamped integer. Empty or non-numeric
// returns the prior canonical value so an interrupted edit reverts
// cleanly on blur instead of falling back to "1".
function parseDate(raw: string, min: number, max: number, fallback: number): number {
  const n = parseInt(raw, 10)
  if (!Number.isFinite(n)) return fallback
  return Math.max(min, Math.min(max, n))
}

export default function AdminSeasonalBackgroundsSection() {
  const { t } = useI18n()
  const [items, setItems] = useState<Record<number, SeasonalBackground>>({})
  // Per-slot raw text for the four date inputs. The canonical numeric
  // state lives in `items`; raw text is what the user is currently
  // typing. Decoupling them lets the user clear the field and retype
  // a different value without the input snapping back to "1" mid-edit
  // (the trap a `Number(value) || 1` onChange would fall into).
  const [rawDates, setRawDates] = useState<Record<number, RawDates>>({})
  // Baseline of saveable fields per slot — what the server most recently
  // confirmed. Used to drive the per-row "Unsaved changes" chip and the
  // disabled state of the row's Save button. Image-upload-only side
  // effects (has_image / image_url) are deliberately not part of this
  // because they're persisted through their own controls.
  const [savedItems, setSavedItems] = useState<Record<number, SeasonalBackground>>({})
  // Errors stay until the admin dismisses them; success messages auto-clear.
  const { msg, setMsg, clearMsg } = useSectionMessage()
  // One row request at a time: a save, a reset or an image upload / removal.
  // Its row is locked until it lands (a save's or a reset's answer rewrites
  // the row's fields; two image requests could land out of order), and the
  // other rows' request controls wait for it.
  const rowRequest = useInFlightKey<{ slot: number; kind: 'save' | 'reset' | 'image' }>()
  const busySlot = rowRequest.key?.slot ?? null
  const savingSlot = rowRequest.key?.kind === 'save' ? rowRequest.key.slot : null
  // Keep the latest `t` in a ref so the load effect can render the
  // failure message in the current locale without listing `t` in its
  // dependency array — depending on `t` re-fetches on every language
  // switch and clobbers any unsaved edits in this section.
  const tRef = useRef(t)
  useEffect(() => { tRef.current = t }, [t])

  function syncRaw(slot: number, sb: SeasonalBackground) {
    setRawDates((prev) => ({ ...prev, [slot]: rawFromSb(sb) }))
  }

  function markSaved(slot: number, sb: SeasonalBackground) {
    setSavedItems((prev) => ({ ...prev, [slot]: sb }))
  }

  // Effective saveable values for `slot` — uses `items` for committed
  // fields, but reparses unblurred raw text for the date inputs so the
  // dirty indicator and disabled state respond mid-edit instead of
  // waiting for blur.
  function rowDirty(slot: number): boolean {
    const sb = items[slot]
    const saved = savedItems[slot]
    if (!sb || !saved) return false
    const raw = rawDates[slot]
    let start_month = sb.start_month
    let start_day = sb.start_day
    let end_month = sb.end_month
    let end_day = sb.end_day
    if (raw) {
      start_month = parseDate(raw.sm, 1, 12, start_month)
      start_day = parseDate(raw.sd, 1, 31, start_day)
      end_month = parseDate(raw.em, 1, 12, end_month)
      end_day = parseDate(raw.ed, 1, 31, end_day)
      start_day = clampDay(start_month, start_day)
      end_day = clampDay(end_month, end_day)
    }
    return (
      sb.enabled !== saved.enabled ||
      sb.label !== saved.label ||
      start_month !== saved.start_month ||
      start_day !== saved.start_day ||
      end_month !== saved.end_month ||
      end_day !== saved.end_day ||
      sb.title_color !== saved.title_color
    )
  }

  // The rows stay locked until the slots have loaded, so a failed load is
  // offered again (Retry) rather than leaving them locked.
  const [loadFailed, setLoadFailed] = useState(false)
  const [loadKey, setLoadKey] = useState(0)
  useEffect(() => {
    const ctrl = new AbortController()
    setLoadFailed(false)
    adminListSeasonalBackgrounds({ signal: ctrl.signal })
      .then((list) => {
        const next: Record<number, SeasonalBackground> = {}
        for (const slot of SLOTS) next[slot] = emptySlot(slot)
        for (const sb of list) next[sb.slot] = sb
        setItems(next)
        setSavedItems(next)
        const rawNext: Record<number, RawDates> = {}
        for (const slot of SLOTS) rawNext[slot] = rawFromSb(next[slot])
        setRawDates(rawNext)
      })
      .catch((err) => {
        // Read `t` through the ref so this effect doesn't have to list
        // it as a dependency — see tRef setup above.
        if (isAbortError(err)) return
        setMsg(tRef.current('adminDash.seasonalBg.loadFailed'), 'error')
        setLoadFailed(true)
      })
    return () => ctrl.abort()
  }, [loadKey])

  function update(slot: number, patch: Partial<SeasonalBackground>) {
    setItems((prev) => {
      const current = prev[slot] || emptySlot(slot)
      const merged = { ...current, ...patch }
      // Keep day-of-month in range when month changes.
      merged.start_day = clampDay(merged.start_month, merged.start_day)
      merged.end_day = clampDay(merged.end_month, merged.end_day)
      return { ...prev, [slot]: merged }
    })
  }

  // Called when a date field loses focus (or before save). Parses the raw
  // text for the named field, clamps both day values against the
  // (possibly newly chosen) month, and writes the result back into both
  // `items` (canonical) and `rawDates` (text) so the input snaps to the
  // committed value.
  function commitDate(slot: number, field: keyof RawDates) {
    const sb = items[slot]
    const raw = rawDates[slot]
    if (!sb || !raw) return
    let { start_month, start_day, end_month, end_day } = sb
    if (field === 'sm') start_month = parseDate(raw.sm, 1, 12, sb.start_month)
    if (field === 'sd') start_day = parseDate(raw.sd, 1, 31, sb.start_day)
    if (field === 'em') end_month = parseDate(raw.em, 1, 12, sb.end_month)
    if (field === 'ed') end_day = parseDate(raw.ed, 1, 31, sb.end_day)
    start_day = clampDay(start_month, start_day)
    end_day = clampDay(end_month, end_day)
    const newSb = { ...sb, start_month, start_day, end_month, end_day }
    setItems((prev) => ({ ...prev, [slot]: newSb }))
    syncRaw(slot, newSb)
  }

  function onSave(slot: number) {
    return rowRequest.run({ slot, kind: 'save' }, () => saveSlot(slot))
  }

  async function saveSlot(slot: number) {
    const sb0 = items[slot]
    if (!sb0) return
    // Flush any unblurred raw date text first, so a save triggered by
    // pressing the row's Save button without leaving the date field
    // doesn't send stale numeric values.
    const raw = rawDates[slot]
    let start_month = sb0.start_month
    let start_day = sb0.start_day
    let end_month = sb0.end_month
    let end_day = sb0.end_day
    if (raw) {
      start_month = parseDate(raw.sm, 1, 12, start_month)
      start_day = parseDate(raw.sd, 1, 31, start_day)
      end_month = parseDate(raw.em, 1, 12, end_month)
      end_day = parseDate(raw.ed, 1, 31, end_day)
      start_day = clampDay(start_month, start_day)
      end_day = clampDay(end_month, end_day)
    }
    // Flush title color too: trim + lowercase mirrors the on-blur
    // normalization, in case the user typed and hit Save before the
    // text input blurred.
    const title_color = sb0.title_color.trim().toLowerCase()
    const sb: SeasonalBackground = { ...sb0, start_month, start_day, end_month, end_day, title_color }
    setItems((prev) => ({ ...prev, [slot]: sb }))
    syncRaw(slot, sb)
    clearMsg()
    try {
      const updated = await adminUpdateSeasonalBackground(slot, {
        enabled: sb.enabled,
        label: sb.label,
        start_month: sb.start_month,
        start_day: sb.start_day,
        end_month: sb.end_month,
        end_day: sb.end_day,
        title_color: sb.title_color,
      })
      setItems((prev) => ({ ...prev, [slot]: { ...prev[slot], ...updated } }))
      syncRaw(slot, { ...sb, ...updated })
      markSaved(slot, { ...sb, ...updated })
      setMsg(t('adminDash.seasonalBg.saved'), 'success')
    } catch (err) {
      setMsg(failureMessage(t('adminDash.seasonalBg.saveFailed'), err, t), 'error')
    }
  }

  async function onUpload(slot: number, e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    if (file.size > UPLOAD_LIMITS.HEADER_IMAGE.bytes) {
      setMsg(t('common.fileTooLarge', { limit: UPLOAD_LIMITS.HEADER_IMAGE.label }), 'error')
      return
    }
    await rowRequest.run({ slot, kind: 'image' }, () => uploadImage(slot, file))
  }

  async function uploadImage(slot: number, file: File) {
    clearMsg()
    try {
      const updated = await adminUploadSeasonalBackgroundImage(slot, file)
      // Merge only image-related fields. Spreading the full server
      // response would overwrite any unsaved local edits (label, dates,
      // enabled, title_color) with the server's prior-save values, since
      // the upload endpoint returns the entire slot dict.
      setItems((prev) => ({
        ...prev,
        [slot]: {
          ...prev[slot],
          image_url: updated.image_url,
          has_image: updated.has_image,
        },
      }))
      setMsg(t('adminDash.seasonalBg.imageUploaded'), 'success')
    } catch (err) {
      setMsg(failureMessage(t('adminDash.seasonalBg.imageUploadFailed'), err, t), 'error')
    }
  }

  function onDeleteImage(slot: number) {
    return rowRequest.run({ slot, kind: 'image' }, () => deleteImage(slot))
  }

  async function deleteImage(slot: number) {
    clearMsg()
    try {
      await adminDeleteSeasonalBackgroundImage(slot)
      setItems((prev) => ({
        ...prev,
        [slot]: { ...prev[slot], image_url: '', has_image: false },
      }))
      setMsg(t('adminDash.seasonalBg.imageRemoved'), 'success')
    } catch {
      setMsg(t('adminDash.seasonalBg.imageRemoveFailed'), 'error')
    }
  }

  async function onResetSlot(slot: number) {
    // The button sits next to Save in the row, so a misclick can wipe
    // label / dates / color / image in one round trip. Confirm first to
    // match the destructive-action treatment elsewhere (slide delete).
    if (!confirm(t('adminDash.seasonalBg.resetRowConfirm', { slot }))) return
    await rowRequest.run({ slot, kind: 'reset' }, () => resetSlot(slot))
  }

  async function resetSlot(slot: number) {
    clearMsg()
    try {
      const reset = await adminResetSeasonalBackground(slot)
      setItems((prev) => ({ ...prev, [slot]: reset }))
      syncRaw(slot, reset)
      markSaved(slot, reset)
      setMsg(t('adminDash.seasonalBg.resetDone'), 'success')
    } catch {
      setMsg(t('adminDash.seasonalBg.resetFailed'), 'error')
    }
  }

  return (
    <div style={{ borderLeft: '3px solid var(--color-border-strong)', paddingLeft: 8, marginTop: 4 }}>
      <div style={subHeading}>{t('adminDash.seasonalBg.section')}</div>
      <div style={{ fontSize: 11, opacity: 0.65, marginBottom: 4 }}>
        {t('adminDash.seasonalBg.help')}
      </div>
      <div style={{ fontSize: 11, opacity: 0.65, marginBottom: 8 }}>
        {t('adminDash.seasonalBg.priorityHelp')}
      </div>
      {loadFailed && (
        <button
          type="button"
          onClick={() => { clearMsg(); setLoadKey((k) => k + 1) }}
          style={{ fontSize: 11, marginBottom: 8 }}
        >
          {t('adminDash.retry')}
        </button>
      )}
      <div style={{ display: 'grid', gap: 8 }}>
        {SLOTS.map((slot) => {
          const sb = items[slot] || emptySlot(slot)
          const saving = savingSlot === slot
          // Another row's request is out: this row's request controls wait.
          const waiting = busySlot !== null && busySlot !== slot
          const rangeInvalid = isRangeInvalid(sb)
          // Color is invalid only when non-empty AND not parseable as
          // "#rrggbb"; empty is intentionally allowed (means "use theme
          // default"). Blocks the row's Save so the user can't submit
          // something the server is guaranteed to 422 on.
          const colorInvalid = sb.title_color !== '' && !isValidHexColor(sb.title_color)
          // Row is "dirty" when any saveable field differs from the
          // server-confirmed baseline. Mirrors the indicator on the main
          // viewer-settings panel so admins don't accidentally switch
          // slots leaving unsaved edits behind.
          const dirty = rowDirty(slot)
          return (
            // Locked while its request is in flight (see rowRequest): a
            // save's or reset's answer replaces the row's fields, so an edit
            // made meanwhile would be overwritten (and marked as saved).
            <LockFieldset
              key={slot}
              // Also until the slots have loaded: the load replaces every row.
              locked={busySlot === slot || !savedItems[slot]}
              style={{
                ...itemRow,
                borderColor: rangeInvalid ? 'var(--color-danger-border)' : 'var(--color-border)',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <strong style={{ fontSize: 12 }}>#{slot}</strong>
                <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 4 }}>
                  <input
                    type="checkbox"
                    checked={sb.enabled}
                    onChange={(e) => update(slot, { enabled: e.target.checked })}
                  />
                  {t('adminDash.seasonalBg.enabled')}
                </label>
                <input
                  value={sb.label}
                  onChange={(e) => update(slot, { label: e.target.value.slice(0, 128) })}
                  placeholder={t('adminDash.seasonalBg.labelPlaceholder')}
                  style={{ flex: 1, fontSize: 12 }}
                />
              </div>

              <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <span style={{ fontSize: 11, opacity: 0.7 }}>{t('adminDash.seasonalBg.from')}</span>
                <input
                  type="number"
                  min={1}
                  max={12}
                  value={rawDates[slot]?.sm ?? String(sb.start_month)}
                  onChange={(e) => setRawDates((prev) => ({ ...prev, [slot]: { ...(prev[slot] ?? rawFromSb(sb)), sm: e.target.value } }))}
                  onBlur={() => commitDate(slot, 'sm')}
                  style={{ width: 48, fontSize: 12 }}
                />
                <span style={{ fontSize: 11 }}>/</span>
                <input
                  type="number"
                  min={1}
                  max={31}
                  value={rawDates[slot]?.sd ?? String(sb.start_day)}
                  onChange={(e) => setRawDates((prev) => ({ ...prev, [slot]: { ...(prev[slot] ?? rawFromSb(sb)), sd: e.target.value } }))}
                  onBlur={() => commitDate(slot, 'sd')}
                  style={{ width: 48, fontSize: 12 }}
                />
                <span style={{ fontSize: 11, opacity: 0.7 }}>{t('adminDash.seasonalBg.to')}</span>
                <input
                  type="number"
                  min={1}
                  max={12}
                  value={rawDates[slot]?.em ?? String(sb.end_month)}
                  onChange={(e) => setRawDates((prev) => ({ ...prev, [slot]: { ...(prev[slot] ?? rawFromSb(sb)), em: e.target.value } }))}
                  onBlur={() => commitDate(slot, 'em')}
                  style={{ width: 48, fontSize: 12 }}
                />
                <span style={{ fontSize: 11 }}>/</span>
                <input
                  type="number"
                  min={1}
                  max={31}
                  value={rawDates[slot]?.ed ?? String(sb.end_day)}
                  onChange={(e) => setRawDates((prev) => ({ ...prev, [slot]: { ...(prev[slot] ?? rawFromSb(sb)), ed: e.target.value } }))}
                  onBlur={() => commitDate(slot, 'ed')}
                  style={{ width: 48, fontSize: 12 }}
                />
                {dirty && !saving && (
                  <span style={{
                    marginLeft: 'auto',
                    fontSize: 10,
                    fontWeight: 600,
                    padding: '1px 6px',
                    borderRadius: 4,
                    background: 'var(--color-warning-bg, #fef3c7)',
                    color: 'var(--color-warning-fg, #92400e)',
                  }}>
                    {t('adminDash.unsavedChanges')}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => onSave(slot)}
                  disabled={saving || waiting || rangeInvalid || colorInvalid || !dirty}
                  style={{ marginLeft: dirty && !saving ? 0 : 'auto', fontSize: 12 }}
                >
                  {saving ? t('adminDash.saving') : t('adminDash.seasonalBg.saveRow')}
                </button>
              </div>

              {rangeInvalid && (
                <div style={{ fontSize: 11, color: 'var(--color-danger-fg)', marginBottom: 6 }}>
                  {t('adminDash.seasonalBg.rangeInvalidYearWrap')}
                </div>
              )}

              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, flexWrap: 'wrap' }}>
                <span style={{ fontSize: 11, opacity: 0.7 }}>{t('adminDash.seasonalBg.titleColorLabel')}</span>
                <input
                  type="color"
                  // The native color picker only accepts a full "#rrggbb"
                  // value; mid-edit text like "#1a" would silently reset
                  // it to the previous value. Fall back to black until
                  // the typed value parses, so the picker stays in sync
                  // and never displays a stale color.
                  value={isValidHexColor(sb.title_color) ? sb.title_color.toLowerCase() : '#000000'}
                  // The HTML spec guarantees this input emits a lowercase
                  // "#rrggbb" string, so no normalization is needed here.
                  onChange={(e) => update(slot, { title_color: e.target.value })}
                  style={{ width: 36, height: 24, padding: 0, border: '1px solid var(--color-border)', borderRadius: 3 }}
                  aria-label={t('adminDash.seasonalBg.titleColorLabel')}
                />
                <input
                  type="text"
                  value={sb.title_color}
                  placeholder="#1a2b3c"
                  // Don't lowercase on every keystroke — typing "F" only to
                  // see "f" land is jarring and disrupts IME / caret. The
                  // canonical lowercased value is produced on blur (and
                  // again at save), keeping the on-disk form server-side
                  // consistent with _normalize_title_color.
                  onChange={(e) => update(slot, { title_color: e.target.value })}
                  onBlur={(e) => update(slot, { title_color: e.target.value.trim().toLowerCase() })}
                  style={{ width: 100, fontFamily: 'monospace', fontSize: 12 }}
                />
                {sb.title_color && (
                  <button
                    type="button"
                    onClick={() => update(slot, { title_color: '' })}
                    style={{ fontSize: 11 }}
                  >
                    {t('adminDash.seasonalBg.clearColor')}
                  </button>
                )}
              </div>
              {sb.title_color !== '' && !isValidHexColor(sb.title_color) && (
                <div style={{ fontSize: 11, color: 'var(--color-danger-fg)', marginBottom: 6 }}>
                  {t('adminDash.invalidColorHex')}
                </div>
              )}

              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                {sb.has_image && sb.image_url ? (
                  <SlotPreview url={sb.image_url} />
                ) : (
                  <div
                    style={{
                      width: 64,
                      height: 32,
                      background: 'var(--color-surface-sunken, #f5f5f5)',
                      border: '1px dashed var(--color-border)',
                      borderRadius: 3,
                      fontSize: 10,
                      color: 'var(--color-text-faint)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                    }}
                  >
                    {t('adminDash.seasonalBg.noImage')}
                  </div>
                )}
                <input
                  type="file"
                  accept=".png,.apng,.gif,image/png,image/gif"
                  onChange={(e) => onUpload(slot, e)}
                  disabled={waiting}
                  style={{ fontSize: 11 }}
                />
                {sb.has_image && (
                  <button
                    type="button"
                    onClick={() => onDeleteImage(slot)}
                    disabled={waiting}
                    style={{ fontSize: 11 }}
                  >
                    {t('adminDash.seasonalBg.removeImage')}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => onResetSlot(slot)}
                  disabled={waiting}
                  style={{ marginLeft: 'auto', fontSize: 11 }}
                  title={t('adminDash.seasonalBg.resetRowHelp')}
                >
                  {t('adminDash.seasonalBg.resetRow')}
                </button>
              </div>
            </LockFieldset>
          )
        })}
      </div>
      <SectionMessage msg={msg} onDismiss={clearMsg} style={{ marginTop: 8 }} />
    </div>
  )
}
