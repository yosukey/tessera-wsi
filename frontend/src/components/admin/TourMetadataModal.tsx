import React, { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useDialogFocus } from '../../hooks/useDialogFocus'
import {
  adminListMetadataOptions,
  adminUpdateTour,
  isAbortError,
  tourConflictOf,
  type MetadataOptions,
  type TourDetail,
  type TourSummary,
} from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { BASE_KEYS, META_DISPLAY_KEYS } from '../../lib/admin/validation'
import { apiErrorMessage } from '../../lib/admin/apiError'
import { metaFieldLabel } from '../../lib/metaLabel'
import { logger } from '../../lib/logger'
import { useInFlight } from '../../hooks/useInFlight'
import { Button, LockFieldset } from '../ui'
import ConflictBanner from './ConflictBanner'
import OptionMultiSelect from './OptionMultiSelect'

type Attrs = Record<string, string[]>

type Props = {
  tour: TourSummary
  // The parent slide's metadata, which the tour inherits; shown read-only.
  slideAttributes: Attrs
  // The save landed: the response's tour (new revision and attributes).
  onSaved: (tour: TourSummary) => void
  // The save was refused because the tour changed elsewhere: the server's
  // current tour and steps, for the editor to adopt (so a retry expects the
  // current revision).
  onConflict: (current: TourDetail) => void
  onClose: () => void
  // Whether the picks differ from what is saved, for the editor's guard
  // against leaving the page (tab close / reload) with unsaved changes.
  onDirtyChange?: (dirty: boolean) => void
}

// The non-empty keys, values sorted, as a stable string: what "unchanged"
// compares against.
function normalise(attrs: Attrs): string {
  const out: Attrs = {}
  for (const k of Object.keys(attrs).sort()) {
    const vals = [...new Set(attrs[k] || [])].filter(Boolean).sort()
    if (vals.length) out[k] = vals
  }
  return JSON.stringify(out)
}

// Edits a tour's OWN metadata. The tour also carries its slide's (a tour's
// metadata is the union of the two), listed read-only at the top so the
// values aren't picked twice. Choices come from the same master list as the
// slide editor's; the server holds instructors to it.
export default function TourMetadataModal({ tour, slideAttributes, onSaved, onConflict, onClose, onDirtyChange }: Props) {
  const { t } = useI18n()
  const [options, setOptions] = useState<MetadataOptions | null>(null)
  const [loadError, setLoadError] = useState(false)
  const [selections, setSelections] = useState<Attrs>(() => ({ ...(tour.attributes || {}) }))
  // What `selections` is compared against for "unsaved changes": the tour's
  // attributes when opened, or as last saved.
  const [baseline, setBaseline] = useState(() => normalise(tour.attributes || {}))
  const saveRequest = useInFlight()
  const saving = saveRequest.busy
  const [msg, setMsg] = useState('')
  // The server's attributes after a save was refused as a conflict.
  const [conflict, setConflict] = useState<Attrs | null>(null)
  const dirty = normalise(selections) !== baseline
  const onDirtyChangeRef = useRef(onDirtyChange)
  onDirtyChangeRef.current = onDirtyChange
  useEffect(() => { onDirtyChangeRef.current?.(dirty) }, [dirty])
  useEffect(() => () => onDirtyChangeRef.current?.(false), [])

  useEffect(() => {
    const ctrl = new AbortController()
    adminListMetadataOptions({ signal: ctrl.signal })
      .then((r) => setOptions(r.options || {}))
      .catch((err) => {
        if (isAbortError(err)) return
        logger.error('metadata options load failed:', err)
        setLoadError(true)
      })
    return () => ctrl.abort()
  }, [])

  function close() {
    // Not while a save is in flight: it would land after the dialog closed.
    if (saving) return
    if (dirty && !confirm(t('adminTour.discardChangesConfirm'))) return
    onClose()
  }
  const closeRef = useRef(close)
  closeRef.current = close

  const panelRef = useRef<HTMLDivElement | null>(null)
  useDialogFocus(panelRef)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') closeRef.current() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // The base categories, then every custom category of the master list, then
  // any key only this tour still carries (so it can be cleared).
  const keys = useMemo(() => {
    const custom = Object.keys(options || {}).filter((k) => !BASE_KEYS.has(k)).sort()
    const known = new Set([...META_DISPLAY_KEYS, ...custom])
    const extra = Object.keys(selections)
      .filter((k) => !known.has(k) && (selections[k] || []).length > 0)
      .sort()
    return [...META_DISPLAY_KEYS, ...custom, ...extra]
  }, [options, selections])

  const inherited = Object.entries(slideAttributes).filter(([, vals]) => vals.length > 0)

  function save() {
    return saveRequest.run(async () => {
      setMsg('')
      const attributes: Attrs = {}
      for (const [k, vals] of Object.entries(selections)) {
        const cleaned = vals.filter(Boolean)
        if (cleaned.length) attributes[k] = cleaned
      }
      try {
        const res = await adminUpdateTour(tour.id, { attributes }, tour.revision)
        setConflict(null)
        setBaseline(normalise(res.tour.attributes || {}))
        onSaved(res.tour)
        onClose()
      } catch (err) {
        const current = tourConflictOf(err)
        if (current) {
          // Keep the user's picks; the parent adopts the server's tour, so
          // "save over it" expects its current revision.
          onConflict(current)
          setConflict(current.tour.attributes || {})
        } else {
          setMsg(apiErrorMessage(err, t) || t('adminTour.metadataSaveFailed'))
        }
      }
    })
  }

  function reloadFromServer() {
    if (!conflict) return
    setSelections({ ...conflict })
    setBaseline(normalise(conflict))
    setConflict(null)
  }

  return createPortal((
    <div
      className="overscroll-panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="tour-metadata-title"
      onClick={close}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.45)',
        zIndex: 'var(--z-modal, 1000)' as unknown as number,
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
        padding: 20, overflowY: 'auto',
      }}
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        style={{
          background: 'var(--color-surface, #fff)', border: '1px solid var(--color-border)',
          borderRadius: 12, padding: 12, width: '100%', maxWidth: 560,
          boxShadow: 'var(--shadow-lg, 0 10px 30px rgba(0,0,0,0.25))', margin: 'auto',
          display: 'grid', gap: 12,
        }}
      >
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <div id="tour-metadata-title" style={{ fontWeight: 800 }}>{t('adminTour.metadataTitle')}</div>
          <button type="button" style={{ marginLeft: 'auto' }} onClick={close} disabled={saving}>{t('adminDash.close')}</button>
        </div>
        <div style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>{t('adminTour.metadataHelp')}</div>

        <section aria-label={t('adminTour.metadataInherited')}>
          <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)', marginBottom: 4 }}>
            {t('adminTour.metadataInherited')}
          </div>
          {inherited.length === 0 ? (
            <div style={{ fontSize: 12, opacity: 0.7 }}>{t('adminTour.metadataInheritedNone')}</div>
          ) : (
            <div style={{ display: 'grid', gap: 2, fontSize: 12 }}>
              {inherited.map(([k, vals]) => (
                <div key={k}>
                  <span style={{ color: 'var(--color-text-muted)' }}>{metaFieldLabel(t, k)}: </span>
                  {vals.join(', ')}
                </div>
              ))}
            </div>
          )}
        </section>

        <section aria-label={t('adminTour.metadataOwn')} style={{ display: 'grid', gap: 10 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--color-text-muted)' }}>
            {t('adminTour.metadataOwn')}
          </div>
          {loadError && <div role="alert" style={{ fontSize: 12, color: 'var(--color-danger-fg)' }}>{t('adminTour.metadataLoadFailed')}</div>}
          {options !== null && (
            // Locked while a save is in flight: a successful save closes the
            // dialog, so a pick made meanwhile (not in the request) would be
            // dropped without a word.
            <LockFieldset locked={saving} style={{ display: 'grid', gap: 10 }}>
              {keys.map((k) => (
                <OptionMultiSelect
                  key={k}
                  label={metaFieldLabel(t, k)}
                  k={k}
                  options={options[k] || []}
                  selected={selections[k] || []}
                  onChange={(vals) => setSelections((s) => ({ ...s, [k]: vals }))}
                  t={t}
                  orphanTitle={(value) => t('adminTour.metadataOrphanValue', { key: k, value })}
                />
              ))}
            </LockFieldset>
          )}
        </section>

        {conflict !== null && (
          <ConflictBanner
            message={t('adminTour.metadataConflict')}
            reloadLabel={t('adminTour.conflictReload')}
            overwriteLabel={t('adminTour.conflictOverwrite')}
            onReload={reloadFromServer}
            onOverwrite={save}
            disabled={saving}
          />
        )}
        {msg && <div role="alert" style={{ fontSize: 12, color: 'var(--color-danger-fg)' }}>{msg}</div>}

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <Button size="sm" variant="secondary" onClick={close} disabled={saving}>{t('adminTour.cancel')}</Button>
          <Button size="sm" variant="primary" disabled={saving || options === null || !dirty} onClick={save}>
            {t('adminTour.metadataSave')}
          </Button>
        </div>
      </div>
    </div>
  ), document.body)
}
