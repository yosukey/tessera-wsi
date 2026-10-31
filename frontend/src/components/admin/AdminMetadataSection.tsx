import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  MetadataOptions,
  MetadataCounts,
  MetadataCountEntry,
  adminAddMetadataOption,
  adminDeleteMetadataKey,
  adminDeleteMetadataOption,
  adminGetMetadataKeyUsage,
  adminGetMetadataOptionUsage,
  adminGetMetadataCounts,
  adminRenameMetadataOption,
  adminUpdateMetadataDisplaySettings,
  isAdminFromToken,
} from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { metaFieldLabel } from '../../lib/metaLabel'
import { BASE_KEYS, META_DISPLAY_KEYS, CTRL_CHAR_RE } from '../../lib/admin/validation'
import { apiErrorMessage } from '../../lib/admin/apiError'
import { logger } from '../../lib/logger'
import { useInFlight, useInFlightKey } from '../../hooks/useInFlight'
import { LockFieldset } from '../ui'
import OrderedAttrList from './OrderedAttrList'

interface Props {
  metaOptions: MetadataOptions
  setMetaOptions: React.Dispatch<React.SetStateAction<MetadataOptions>>
  customMetaKeys: string[]
  refreshMetadataOptions: () => Promise<void> | void
  // Re-fetches the dashboard's slide list. A value rename / delete and a
  // category delete rewrite slide_attributes rows server-side, so the list
  // (and the edit modal / bulk-category modal seeded from it) must be reloaded
  // — otherwise the next save from a stale slide writes the old value back.
  refreshSlides: () => Promise<void>
  // The same for the dashboard's tour list: tours carry their own metadata
  // (and a revision the rewrite bumps), rewritten by the same operations.
  refreshTours: () => Promise<unknown>
  // Three viewer-settings JSON lists owned by this tab: which categories
  // appear in the viewer panel / top-page card / top-page filter, and in
  // what order. Lifted to the parent so a tab switch doesn't drop unsaved
  // edits, mirroring how AdminViewerSettingsSection lifts its baseline.
  vsExtraAttrs: string[]
  setVsExtraAttrs: React.Dispatch<React.SetStateAction<string[]>>
  vsIndexCardAttrs: string[]
  setVsIndexCardAttrs: React.Dispatch<React.SetStateAction<string[]>>
  vsIndexFilterAttrs: string[]
  setVsIndexFilterAttrs: React.Dispatch<React.SetStateAction<string[]>>
  // JSON snapshot of the three lists the server last persisted, used to
  // light up the dirty indicator and gate the Save button.
  mdSavedSnapshot: string
  setMdSavedSnapshot: (v: string) => void
  // True once the parent has actually fetched metadata options. Until
  // then, customMetaKeys is the initial [] and orphan-key cleanup must
  // wait — otherwise every legitimate custom key would be misclassified
  // as orphan and silently stripped on the first save.
  metaOptionsLoaded: boolean
  // Monotonic counter the parent bumps after a slide save / bulk category
  // assignment. Watching it lets the count badges refresh live (without a
  // page reload) when metadata is assigned to a WSI in the main pane while
  // this tab is open.
  countsRefreshSignal: number
  // The display lists load with the viewer settings; when that load failed
  // they stay locked, so say so here and offer it again.
  displayLoadError?: boolean
  onRetryDisplayLoad?: () => void
}

// The counts endpoint lists only the keys / values some slide carries, so once
// it has loaded, a missing entry means "used by no slide": the 0 badge and the
// unused highlight are exactly for those.
const UNUSED: MetadataCountEntry = { total: 0, published: 0 }

function snapshotDisplay(extra: string[], card: string[], filter: string[]): string {
  return JSON.stringify({ extra, card, filter })
}

export default function AdminMetadataSection({
  metaOptions, setMetaOptions, customMetaKeys, refreshMetadataOptions, refreshSlides, refreshTours,
  vsExtraAttrs, setVsExtraAttrs,
  vsIndexCardAttrs, setVsIndexCardAttrs,
  vsIndexFilterAttrs, setVsIndexFilterAttrs,
  mdSavedSnapshot, setMdSavedSnapshot,
  metaOptionsLoaded,
  countsRefreshSignal,
  displayLoadError = false,
  onRetryDisplayLoad,
}: Props) {
  const { t } = useI18n()
  const canEditDisplaySettings = isAdminFromToken()
  const [metaMsg, setMetaMsg] = useState('')
  const [mdSaving, setMdSaving] = useState(false)
  const [mdMsg, setMdMsg] = useState('')
  const [metaNewValue, setMetaNewValue] = useState<Record<string, string>>({})
  // The category key whose "Add option" POST is currently in flight. One at a
  // time, so every category's Add (and box) waits for it: a double-click
  // would fire two POSTs — the second's duplicate rejection would otherwise
  // clobber the first's success with a spurious error — and the success
  // clears the box, which would wipe text typed after the click.
  const addRequest = useInFlightKey<string>()
  const metaAddBusyKey = addRequest.key
  const [metaNewCategory, setMetaNewCategory] = useState('')
  // The value currently being inline-renamed, and its edit-box text.
  const [editingValue, setEditingValue] = useState<{ key: string; value: string } | null>(null)
  const [editingValueText, setEditingValueText] = useState('')
  // A rename in flight. The editor is locked (and no other value can be
  // opened for renaming) until it lands: the success closes the editor, which
  // would drop text typed meanwhile, and a repeat Enter would send it twice.
  const renameRequest = useInFlight()
  const renameBusy = renameRequest.busy
  // A value's or a category's delete, from its usage probe to the refresh.
  // The deletes, renames and adds wait for it: a second click would send a
  // second DELETE, and a rename or add of what is being deleted would race it.
  const deleteRequest = useInFlight()
  const deleteBusy = deleteRequest.busy
  // Categories the user just typed into the "Add category" form but for which
  // no value has been persisted yet. The backend can only represent a custom
  // category through its MetadataOption rows, so an empty category has no
  // server-side existence — without this local slice, `refreshMetadataOptions`
  // would silently wipe a freshly-added category before the user could put a
  // value into it.
  const [pendingKeys, setPendingKeys] = useState<string[]>([])
  const [counts, setCounts] = useState<MetadataCounts | null>(null)

  const refreshCounts = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await adminGetMetadataCounts({ signal })
      setCounts(data)
    } catch {
      // Non-critical; counts will just be absent
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    refreshCounts(controller.signal)
    return () => controller.abort()
  }, [refreshCounts])

  // Live-refresh when a slide's metadata is assigned elsewhere in the
  // dashboard. The parent bumps countsRefreshSignal after a slide save /
  // bulk category assignment; re-pull counts (and options, since a
  // newly-assigned unregistered value shows up as an orphan pill). Skip the
  // very first run so it doesn't double-fetch alongside the mount effect above.
  const firstSignal = useRef(true)
  useEffect(() => {
    if (firstSignal.current) {
      firstSignal.current = false
      return
    }
    refreshCounts()
    refreshMetadataOptions()
    // refreshCounts is a stable useCallback; refreshMetadataOptions comes from
    // the parent. Keyed intentionally only on the signal to fire exactly once
    // per assignment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [countsRefreshSignal])

  // Reload the slide list after a mutation that rewrote slide attributes.
  // Best-effort: the mutation itself already succeeded, so a failed refresh
  // is logged rather than reported as a failed rename / delete.
  async function refreshSlidesAfterAttrChange() {
    try {
      await refreshSlides()
    } catch (err) {
      logger.error('refreshSlides after metadata change failed:', err)
    }
  }

  async function refreshToursAfterAttrChange() {
    try {
      await refreshTours()
    } catch (err) {
      logger.error('refreshTours after metadata change failed:', err)
    }
  }

  // The warning for deleting a value / category in use, or null when nothing
  // uses it (the caller then asks a plain confirmation instead — every delete
  // of a saved value / category is confirmed): the slides-only wording when only slides use it, a
  // tours-only one when only tours do, and one naming both otherwise.
  function deleteWarning(
    kind: 'option' | 'category',
    name: string,
    usage: { slide_count: number; published_slide_count?: number; tour_count?: number },
  ): string | null {
    const slides = usage.slide_count
    const drafts = slides - (usage.published_slide_count ?? slides)
    const tours = usage.tour_count ?? 0
    if (tours > 0 && slides === 0) {
      return kind === 'option'
        ? t('adminDash.metaDeleteOptionWarnTours', { value: name, tours })
        : t('adminDash.metaDeleteCategoryWarnTours', { key: name, tours })
    }
    if (tours > 0) {
      return kind === 'option'
        ? t('adminDash.metaDeleteOptionWarnSlidesTours', { value: name, slides, drafts, tours })
        : t('adminDash.metaDeleteCategoryWarnSlidesTours', { key: name, slides, drafts, tours })
    }
    if (slides === 0) return null
    if (kind === 'option') {
      return drafts > 0
        ? t('adminDash.metaDeleteOptionWarnSlidesWithDrafts', { value: name, count: slides, drafts })
        : t('adminDash.metaDeleteOptionWarnSlides', { value: name, count: slides })
    }
    return drafts > 0
      ? t('adminDash.metaDeleteCategoryWarnSlidesWithDrafts', { key: name, count: slides, drafts })
      : t('adminDash.metaDeleteCategoryWarnSlides', { key: name, count: slides })
  }

  function changedRetryMessage(result: { slide_count?: number; tour_count?: number }): string {
    const tours = result.tour_count ?? 0
    return tours > 0
      ? t('adminDash.metaDeleteChangedRetryTours', { slides: result.slide_count ?? 0, tours })
      : t('adminDash.metaDeleteChangedRetry', { count: result.slide_count ?? 0 })
  }

  function onAddMetaOption(key: string) {
    const value = (metaNewValue[key] || '').trim()
    if (!value) return
    return addRequest.run(key, async () => {
      setMetaMsg('')
      try {
        const res = await adminAddMetadataOption(key, value)
        if (res?.duplicate) {
          setMetaMsg(t('adminDash.metaDuplicateValue', { key, value }))
          return
        }
        setMetaNewValue((s) => ({ ...s, [key]: '' }))
        // The category now has a real MetadataOption row; the next refresh will
        // surface it through customMetaKeys, so drop it from the local-only set.
        setPendingKeys((ks) => ks.filter((k) => k !== key))
        await refreshMetadataOptions()
        await refreshCounts()
      } catch (err: any) {
        setMetaMsg(apiErrorMessage(err, t) || t('adminDash.saveFailed'))
      }
    })
  }

  async function onDeleteMetaOption(key: string, value: string) {
    await deleteRequest.run(async () => {
      setMetaMsg('')
      try {
        const usage = await adminGetMetadataOptionUsage(key, value)
        const msg = deleteWarning('option', value, usage) ?? t('adminDash.metaDeleteOptionConfirm', { key: metaFieldLabel(t, key), value })
        if (!confirm(msg)) return
        // Conditional on the slide set the confirmation described: another
        // admin (or a slide save) may have changed which slides carry the value
        // since the probe, and the server then deletes nothing (409 `changed`)
        // instead of stripping slides nobody confirmed.
        const result = await adminDeleteMetadataOption(key, value, usage.slides_fingerprint)
        if (result?.changed) {
          setMetaMsg(changedRetryMessage(result))
          await refreshCounts()
          return
        }
        const affected = result?.slides_affected ?? 0
        await refreshMetadataOptions()
        await refreshCounts()
        if (affected > 0) await refreshSlidesAfterAttrChange()
        if ((result?.tours_affected ?? 0) > 0) await refreshToursAfterAttrChange()
      } catch (err: any) {
        setMetaMsg(apiErrorMessage(err, t) || t('adminDash.saveFailed'))
      }
    })
  }

  function startRenameValue(key: string, value: string) {
    setMetaMsg('')
    setEditingValue({ key, value })
    setEditingValueText(value)
  }

  function cancelRenameValue() {
    setEditingValue(null)
    setEditingValueText('')
  }

  function onRenameValue(key: string, oldValue: string) {
    if (renameBusy || deleteBusy) return
    const newValue = editingValueText.trim()
    if (!newValue || newValue === oldValue) { cancelRenameValue(); return }
    return renameRequest.run(() => renameValue(key, oldValue, newValue))
  }

  async function renameValue(key: string, oldValue: string, newValue: string) {
    setMetaMsg('')
    try {
      const res = await adminRenameMetadataOption(key, oldValue, newValue)
      // Reject policy: the backend returns 409 with target_exists when the new
      // name already exists under this key. Reuse the duplicate-value message.
      if (res?.target_exists) {
        setMetaMsg(t('adminDash.metaDuplicateValue', { key, value: newValue }))
        return
      }
      cancelRenameValue()
      await refreshMetadataOptions()
      await refreshCounts()
      if ((res?.slides_affected ?? 1) > 0) await refreshSlidesAfterAttrChange()
      if ((res?.tours_affected ?? 0) > 0) await refreshToursAfterAttrChange()
    } catch (err: any) {
      setMetaMsg(apiErrorMessage(err, t) || t('adminDash.saveFailed'))
    }
  }

  async function onAddMetaCategory() {
    const key = metaNewCategory.trim()
    if (!key || deleteBusy) return
    if (BASE_KEYS.has(key)) { setMetaMsg(t('adminDash.metaBaseKeyReserved')); return }
    if (/[,:]/.test(key)) { setMetaMsg(t('adminDash.validationAttrKeyInvalidChars', { key })); return }
    if (key.length > 128) { setMetaMsg(t('adminDash.validationAttrKeyMaxLen', { key: key.slice(0, 30), max: 128 })); return }
    if (CTRL_CHAR_RE.test(key)) { setMetaMsg(t('adminDash.validationAttrKeyCtrlChar', { key: key.slice(0, 30) })); return }
    setMetaOptions((s) => (s[key] ? s : { ...s, [key]: [] }))
    setPendingKeys((ks) => (ks.includes(key) ? ks : [...ks, key]))
    setMetaNewCategory('')
    setMetaMsg('')
  }

  async function onDeleteMetaCategory(key: string) {
    if (BASE_KEYS.has(key)) return
    setMetaMsg('')
    // A pending (local-only) category has no server-side representation, so
    // skip the usage probe + DELETE round-trip and just drop it locally.
    if (pendingKeys.includes(key) && (metaOptions[key] || []).length === 0) {
      setPendingKeys((ks) => ks.filter((k) => k !== key))
      setMetaOptions((s) => {
        if (!(key in s)) return s
        const { [key]: _, ...rest } = s
        return rest
      })
      return
    }
    await deleteRequest.run(async () => {
      try {
        const usage = await adminGetMetadataKeyUsage(key)
        const msg = deleteWarning('category', key, usage) ?? t('adminDash.metaDeleteCategoryConfirm', { key })
        if (!confirm(msg)) return
        // Conditional, as for a single value (see onDeleteMetaOption).
        const result = await adminDeleteMetadataKey(key, usage.slides_fingerprint)
        if (result?.changed) {
          setMetaMsg(changedRetryMessage(result))
          await refreshCounts()
          return
        }
        const affected = result?.slides_affected ?? 0
        setPendingKeys((ks) => ks.filter((k) => k !== key))
        await refreshMetadataOptions()
        await refreshCounts()
        if (affected > 0) await refreshSlidesAfterAttrChange()
        if ((result?.tours_affected ?? 0) > 0) await refreshToursAfterAttrChange()
      } catch (err: any) {
        setMetaMsg(apiErrorMessage(err, t) || t('adminDash.saveFailed'))
      }
    })
  }

  // Orphan cleanup: a category deleted earlier in this tab may still be
  // referenced in extra_attrs / index_card_attrs / index_filter_attrs.
  // Skip the filter until customMetaKeys actually reflects the server,
  // otherwise the first save races the metadata-options fetch and strips
  // legitimate custom keys.
  const validKeys = metaOptionsLoaded
    ? new Set([...META_DISPLAY_KEYS, ...customMetaKeys])
    : null
  const effExtra = validKeys ? vsExtraAttrs.filter(k => validKeys.has(k)) : vsExtraAttrs
  const effCard = validKeys ? vsIndexCardAttrs.filter(k => validKeys.has(k)) : vsIndexCardAttrs
  const effFilter = validKeys ? vsIndexFilterAttrs.filter(k => validKeys.has(k)) : vsIndexFilterAttrs
  const mdCurrentSnapshot = snapshotDisplay(effExtra, effCard, effFilter)
  const mdIsDirty = !!mdSavedSnapshot && mdCurrentSnapshot !== mdSavedSnapshot
  const orphans = validKeys
    ? Array.from(new Set([
        ...vsExtraAttrs.filter(k => !validKeys.has(k)),
        ...vsIndexCardAttrs.filter(k => !validKeys.has(k)),
        ...vsIndexFilterAttrs.filter(k => !validKeys.has(k)),
      ]))
    : []

  async function onSaveDisplaySettings() {
    if (mdSaving) return
    setMdMsg('')
    setMdSaving(true)
    try {
      // Strip orphan keys before submission so a deleted category doesn't
      // round-trip back to the server every time the admin saves.
      const cleanedExtra = effExtra
      const cleanedCard = effCard
      const cleanedFilter = effFilter
      if (cleanedExtra.length !== vsExtraAttrs.length) setVsExtraAttrs(cleanedExtra)
      if (cleanedCard.length !== vsIndexCardAttrs.length) setVsIndexCardAttrs(cleanedCard)
      if (cleanedFilter.length !== vsIndexFilterAttrs.length) setVsIndexFilterAttrs(cleanedFilter)
      await adminUpdateMetadataDisplaySettings({
        extra_attrs: cleanedExtra,
        index_card_attrs: cleanedCard,
        index_filter_attrs: cleanedFilter,
      })
      setMdSavedSnapshot(snapshotDisplay(cleanedExtra, cleanedCard, cleanedFilter))
      setMdMsg(t('adminDash.saved'))
    } catch (err: any) {
      setMdMsg(apiErrorMessage(err, t) || t('adminDash.saveFailed'))
    } finally {
      setMdSaving(false)
    }
  }

  return (
    <>
      <h3 style={{ marginTop: 16 }}>{t('adminDash.metadataTitle')}</h3>
      <div style={{ fontSize: 12, opacity: 0.8, marginBottom: 10 }}>{t('adminDash.metadataHelp')}</div>
      <div style={{ display: 'grid', gap: 14 }}>
        {(() => {
          // Merge customMetaKeys with pending (local-only) keys, dedupe, and
          // keep a stable alphabetical order so the freshly-added category
          // does not jump around as values are added.
          const seen = new Set(META_DISPLAY_KEYS)
          const extras: string[] = []
          for (const k of customMetaKeys) {
            if (!seen.has(k)) { seen.add(k); extras.push(k) }
          }
          for (const k of pendingKeys) {
            if (!seen.has(k)) { seen.add(k); extras.push(k) }
          }
          extras.sort()
          return [...META_DISPLAY_KEYS, ...extras]
        })().map((key) => {
          const vals = metaOptions[key] || []
          const isBase = BASE_KEYS.has(key)
          // A custom category exists server-side only through its
          // option rows; an empty pending key has no DB representation
          // and will silently disappear on the next refresh / page
          // reload. Surface that explicitly so the admin knows they
          // need to add at least one value before navigating away.
          const isPending = !isBase && pendingKeys.includes(key) && vals.length === 0
          const label = metaFieldLabel(t, key)
          const keyCounts = counts ? (counts.keys[key] ?? UNUSED) : undefined
          const valueCounts = counts?.values[key]
          // Tours count by their own metadata; a category or value only tours
          // use is in use, not an orphan.
          const keyTourCounts = counts ? (counts.tour_keys[key] ?? UNUSED) : undefined
          const valueTourCounts = counts?.tour_values[key]
          const keyUnused = keyCounts !== undefined && keyCounts.total === 0 && (keyTourCounts?.total ?? 0) === 0
          return (
            <div key={key} style={{ borderLeft: `3px ${isPending ? 'dashed' : 'solid'} var(--color-border-strong)`, paddingLeft: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
                <div style={{ fontSize: 13, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'inherit' }}>
                  {label}
                </div>
                {isPending && (
                  <span style={{ fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 4, background: 'var(--color-warning-bg, #fef3c7)', color: 'var(--color-warning-fg, #92400e)' }}>
                    {t('adminDash.metaCategoryUnsaved')}
                  </span>
                )}
                {keyCounts !== undefined && (
                  <span
                    title={t('adminDash.metaKeyUsageTitle', { total: keyCounts.total, published: keyCounts.published })}
                    style={{
                      fontSize: 10,
                      fontWeight: 700,
                      padding: '1px 6px',
                      borderRadius: 10,
                      background: keyUnused ? 'var(--color-danger-bg, #fee2e2)' : 'var(--color-surface-muted)',
                      color: keyUnused ? 'var(--color-danger-500, #ef4444)' : 'inherit',
                      opacity: keyUnused ? 1 : 0.75,
                    }}
                  >
                    {t('adminDash.metaKeyUsageCount', { total: keyCounts.total, published: keyCounts.published })}
                  </span>
                )}
                {keyTourCounts !== undefined && keyTourCounts.total > 0 && (
                  <span
                    title={t('adminDash.metaTourUsageTitle', { total: keyTourCounts.total, published: keyTourCounts.published })}
                    style={{
                      fontSize: 10,
                      fontWeight: 700,
                      padding: '0 6px',
                      borderRadius: 10,
                      border: '1px solid var(--color-border-strong)',
                      opacity: 0.75,
                    }}
                  >
                    {t('adminDash.metaTourUsageCount', { total: keyTourCounts.total, published: keyTourCounts.published })}
                  </span>
                )}
                {!isBase && (
                  <button type="button" onClick={() => onDeleteMetaCategory(key)} disabled={deleteBusy || renameBusy || metaAddBusyKey !== null} style={{ marginLeft: 'auto', fontSize: 11 }}>
                    {t('adminDash.metaDeleteCategory')}
                  </button>
                )}
              </div>
              {vals.length > 0 ? (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 6 }}>
                  {vals.map((v) => {
                    const vc = counts ? (valueCounts?.[v] ?? UNUSED) : undefined
                    const tvc = counts ? (valueTourCounts?.[v] ?? UNUSED) : undefined
                    const isOrphan = vc !== undefined && vc.total === 0 && (tvc?.total ?? 0) === 0
                    const isEditing = editingValue?.key === key && editingValue?.value === v
                    if (isEditing) {
                      return (
                        <span
                          key={v}
                          style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: 4,
                            padding: '2px 4px',
                            border: '1px solid var(--color-brand-600)',
                            borderRadius: 12,
                            background: 'var(--color-surface-muted)',
                          }}
                        >
                          <input
                            autoFocus
                            value={editingValueText}
                            disabled={renameBusy || deleteBusy}
                            onChange={(e) => setEditingValueText(e.target.value)}
                            style={{ fontSize: 12, minWidth: 0, width: 120 }}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); onRenameValue(key, v) }
                              else if (e.key === 'Escape') { e.preventDefault(); cancelRenameValue() }
                            }}
                          />
                          <button
                            type="button"
                            aria-label={t('adminDash.metaRenameConfirm')}
                            title={t('adminDash.metaRenameConfirm')}
                            disabled={renameBusy || deleteBusy}
                            onClick={() => onRenameValue(key, v)}
                            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-brand-600)', padding: 0, fontSize: 13 }}
                          >✓</button>
                          <button
                            type="button"
                            aria-label={t('adminDash.metaRenameCancel')}
                            title={t('adminDash.metaRenameCancel')}
                            disabled={renameBusy || deleteBusy}
                            onClick={cancelRenameValue}
                            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-danger-500)', padding: 0, fontSize: 13 }}
                          >✕</button>
                        </span>
                      )
                    }
                    return (
                      <span
                        key={v}
                        style={{
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: 4,
                          fontSize: 12,
                          padding: '2px 6px',
                          border: `1px solid ${isOrphan ? 'var(--color-danger-500, #ef4444)' : 'var(--color-border)'}`,
                          borderRadius: 12,
                          background: isOrphan ? 'var(--color-danger-bg, #fee2e2)' : 'var(--color-surface-muted)',
                        }}
                      >
                        {v}
                        {vc !== undefined && (
                          <span
                            title={t('adminDash.metaValueUsageTitle', { total: vc.total, published: vc.published })}
                            style={{
                              fontSize: 10,
                              fontWeight: 700,
                              minWidth: 14,
                              textAlign: 'center',
                              padding: '0 3px',
                              borderRadius: 8,
                              background: isOrphan ? 'var(--color-danger-500, #ef4444)' : 'var(--color-border-strong)',
                              color: isOrphan ? '#fff' : 'inherit',
                              opacity: isOrphan ? 1 : 0.8,
                            }}
                          >
                            {vc.total}
                          </span>
                        )}
                        {tvc !== undefined && tvc.total > 0 && (
                          <span
                            title={t('adminDash.metaTourUsageTitle', { total: tvc.total, published: tvc.published })}
                            style={{
                              fontSize: 10,
                              fontWeight: 700,
                              minWidth: 14,
                              textAlign: 'center',
                              padding: '0 3px',
                              borderRadius: 8,
                              border: '1px solid var(--color-border-strong)',
                              opacity: 0.8,
                            }}
                          >
                            {t('adminDash.metaTourUsageBadge', { total: tvc.total })}
                          </span>
                        )}
                        <button
                          type="button"
                          aria-label={t('adminDash.metaRenameValue')}
                          title={t('adminDash.metaRenameValue')}
                          disabled={renameBusy || deleteBusy}
                          onClick={() => startRenameValue(key, v)}
                          style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'inherit', padding: 0, fontSize: 11, opacity: 0.7 }}
                        >✎</button>
                        <button type="button" onClick={() => onDeleteMetaOption(key, v)} disabled={deleteBusy || renameBusy || metaAddBusyKey !== null} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--color-danger-500)', padding: 0, fontSize: 12 }}>×</button>
                      </span>
                    )
                  })}
                </div>
              ) : (
                <div style={{ fontSize: 11, opacity: 0.7, marginBottom: 6, color: isPending ? 'var(--color-warning-fg, #92400e)' : undefined }}>
                  {isPending ? t('adminDash.metaCategoryNeedsValue') : t('adminDash.metaNoValues')}
                </div>
              )}
              <LockFieldset locked={metaAddBusyKey !== null || deleteBusy} style={{ display: 'flex', gap: 6 }}>
                <input
                  value={metaNewValue[key] || ''}
                  onChange={(e) => setMetaNewValue((s) => ({ ...s, [key]: e.target.value }))}
                  placeholder={t('adminDash.metaAddValuePlaceholder')}
                  style={{ flex: 1, fontSize: 12, minWidth: 0 }}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); onAddMetaOption(key) } }}
                />
                <button type="button" onClick={() => onAddMetaOption(key)} style={{ fontSize: 12 }}>{t('adminDash.add')}</button>
              </LockFieldset>
            </div>
          )
        })}
      </div>

      <div style={{ marginTop: 18, borderLeft: '3px solid var(--color-border-strong)', paddingLeft: 8 }}>
        <div style={{ fontSize: 13, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'inherit', marginBottom: 6 }}>
          {t('adminDash.metaAddCategory')}
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          <input
            value={metaNewCategory}
            // Not while a delete is out: re-adding the category being deleted
            // would be dropped again when the delete lands.
            disabled={deleteBusy}
            onChange={(e) => setMetaNewCategory(e.target.value)}
            placeholder={t('adminDash.metaAddCategoryPlaceholder')}
            style={{ flex: 1, fontSize: 12, minWidth: 0 }}
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); onAddMetaCategory() } }}
          />
          <button type="button" onClick={onAddMetaCategory} disabled={deleteBusy} style={{ fontSize: 12 }}>{t('adminDash.add')}</button>
        </div>
      </div>

      {metaMsg && (
        <div style={{ marginTop: 10, fontSize: 12, padding: '4px 6px', borderRadius: 4, whiteSpace: 'pre-line', background: 'var(--color-danger-bg)' }}>
          {metaMsg}
        </div>
      )}

      {/* The display lists live in the viewer settings, which are admin-only
          (GET and PATCH /api/admin/viewer/settings). A manager sees this tab
          for the options above, but could neither load nor save the lists. */}
      {canEditDisplaySettings && (<>
      <h3 style={{ marginTop: 32 }}>{t('adminDash.metadataDisplayTitle')}</h3>
      <div style={{ fontSize: 12, opacity: 0.8, marginBottom: 12 }}>{t('adminDash.metadataDisplayHelp')}</div>
      {displayLoadError && (
        <div role="alert" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, fontSize: 12, color: 'var(--color-danger-fg)' }}>
          <span style={{ flex: 1 }}>{t('adminDash.settingsLoadFailed')}</span>
          {onRetryDisplayLoad && (
            <button type="button" onClick={onRetryDisplayLoad} style={{ fontSize: 11 }}>{t('adminDash.retry')}</button>
          )}
        </div>
      )}
      {/* Locked until the lists have loaded (with the viewer settings): a
          change made before that would be overwritten by the load. */}
      <LockFieldset locked={!mdSavedSnapshot} style={{ display: 'grid', gap: 14 }}>
        <div style={{ border: '1px solid var(--color-border)', borderRadius: 5, padding: '8px 10px' }}>
          <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.extraAttrsLabel')}</div>
          <div style={{ fontSize: 11, opacity: 0.7, marginBottom: 6 }}>{t('adminDash.extraAttrsHelp')}</div>
          <OrderedAttrList
            selected={vsExtraAttrs}
            allKeys={META_DISPLAY_KEYS}
            customKeys={customMetaKeys}
            t={t}
            onChange={setVsExtraAttrs}
          />
        </div>

        <div style={{ border: '1px solid var(--color-border)', borderRadius: 5, padding: '8px 10px' }}>
          <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.indexCardAttrsLabel')}</div>
          <div style={{ fontSize: 11, opacity: 0.7, marginBottom: 6 }}>{t('adminDash.indexCardAttrsHelp')}</div>
          <OrderedAttrList
            selected={vsIndexCardAttrs}
            allKeys={META_DISPLAY_KEYS}
            customKeys={customMetaKeys}
            t={t}
            onChange={setVsIndexCardAttrs}
          />
        </div>

        <div style={{ border: '1px solid var(--color-border)', borderRadius: 5, padding: '8px 10px' }}>
          <div style={{ fontSize: 12, marginBottom: 4 }}>{t('adminDash.indexFilterAttrsLabel')}</div>
          <div style={{ fontSize: 11, opacity: 0.7, marginBottom: 6 }}>{t('adminDash.indexFilterAttrsHelp')}</div>
          <OrderedAttrList
            selected={vsIndexFilterAttrs}
            allKeys={META_DISPLAY_KEYS}
            customKeys={customMetaKeys}
            t={t}
            onChange={setVsIndexFilterAttrs}
          />
        </div>

        {orphans.length > 0 && (
          // Orphan-key notice: server still references categories that
          // were deleted above. The Save handler quietly strips them, but
          // without this hint admins see a permanent dirty indicator they
          // can't trace to any visible edit.
          <div style={{ fontSize: 11, color: 'var(--color-warning-fg, #92400e)' }}>
            {t('adminDash.extraAttrsOrphans', { keys: orphans.join(', ') })}
          </div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <button
            type="button"
            onClick={onSaveDisplaySettings}
            disabled={mdSaving || !mdIsDirty}
            style={mdIsDirty ? {
              background: 'var(--color-brand-600)',
              color: 'var(--color-on-brand, #fff)',
              borderColor: 'var(--color-brand-600)',
            } : undefined}
          >
            {mdSaving ? t('adminDash.saving') : t('adminDash.saveDisplaySettings')}
          </button>
          {mdIsDirty && !mdSaving && (
            <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-warning-fg, #92400e)' }}>
              {t('adminDash.unsavedChanges')}
            </span>
          )}
          {mdMsg && (
            <span style={{ fontSize: 12 }}>{mdMsg}</span>
          )}
        </div>
      </LockFieldset>
      </>)}
    </>
  )
}
