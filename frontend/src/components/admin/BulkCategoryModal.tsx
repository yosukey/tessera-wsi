import React, { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useDialogFocus } from '../../hooks/useDialogFocus'
import { useI18n } from '../../lib/i18n'
import { META_DISPLAY_KEYS } from '../../lib/admin/validation'
import { metaFieldLabel } from '../../lib/metaLabel'
import { Button, LockFieldset, Select } from '../ui'
import OptionMultiSelect from './OptionMultiSelect'

type Mode = 'add' | 'remove'

type Props = {
  // What the operation applies to: the selected visible slides, or the
  // selected tours (whose OWN metadata it changes — what a tour inherits
  // from its slide is not touched).
  target: 'slides' | 'tours'
  // The slides / tours the operation will apply to (selected ∩ visible).
  // Their attributes give per-value coverage so the admin can see that the
  // selection is heterogeneous (e.g. "H&E (3/10)") before committing.
  items: { attributes?: Record<string, string[]> | null }[]
  metaOptions: Record<string, string[]>
  customMetaKeys: string[]
  onApply: (key: string, values: string[], mode: Mode) => void
  onClose: () => void
  // True while the applied change is in flight: Apply is disabled so a
  // second click can't send it again (for tours, from revisions the first
  // has already moved past, which would come back as conflicts).
  busy?: boolean
}

export default function BulkCategoryModal({ target, items, metaOptions, customMetaKeys, onApply, onClose, busy = false }: Props) {
  const { t } = useI18n()
  const keys = useMemo(() => [...META_DISPLAY_KEYS, ...customMetaKeys], [customMetaKeys])
  const [key, setKey] = useState<string>(keys[0] || '')
  const [mode, setMode] = useState<Mode>('add')
  const [values, setValues] = useState<string[]>([])

  // Switching key/mode invalidates the picked values.
  useEffect(() => { setValues([]) }, [key, mode])

  const panelRef = useRef<HTMLDivElement | null>(null)
  useDialogFocus(panelRef)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [onClose])

  const total = items.length

  // How many of the targets already carry each value under `key`.
  const coverage = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const item of items) {
      for (const v of (item.attributes || {})[key] || []) {
        counts[v] = (counts[v] || 0) + 1
      }
    }
    return counts
  }, [items, key])

  // Candidate values:
  //  - add: every registered option for the key, plus any orphan value
  //    already present on a target.
  //  - remove: only values at least one target carries (others are
  //    no-ops, so showing them would just mislead).
  const candidates = useMemo(() => {
    const registered = metaOptions[key] || []
    if (mode === 'remove') {
      return Object.keys(coverage).sort((a, b) => a.localeCompare(b))
    }
    return Array.from(new Set([...registered, ...Object.keys(coverage)]))
      .sort((a, b) => a.localeCompare(b))
  }, [metaOptions, key, mode, coverage])

  // Annotate each candidate with its coverage so OptionMultiSelect surfaces
  // the (n/total) badge. The label is display-only; the picked value passed
  // back to onApply is still the bare value.
  const labelFor = (v: string) => `${v} (${coverage[v] || 0}/${total})`
  const displayOptions = useMemo(() => candidates.map(labelFor), [candidates, total, coverage])
  const displaySelected = useMemo(() => values.map(labelFor), [values, total, coverage])
  const labelToValue = useMemo(() => {
    const m = new Map<string, string>()
    for (const v of candidates) m.set(labelFor(v), v)
    return m
  }, [candidates, total, coverage])

  const onMultiChange = (nextLabels: string[]) => {
    setValues(nextLabels.map(l => labelToValue.get(l) ?? l))
  }

  return createPortal((
    <div
      className="overscroll-panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="bulk-category-title"
      onClick={onClose}
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
        onClick={e => e.stopPropagation()}
        style={{
          background: 'var(--color-surface, #fff)', border: '1px solid var(--color-border)',
          borderRadius: 12, padding: 12, width: '100%', maxWidth: 520,
          boxShadow: 'var(--shadow-lg, 0 10px 30px rgba(0,0,0,0.25))', margin: 'auto',
        }}
      >
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <div id="bulk-category-title" style={{ fontWeight: 800 }}>
            {target === 'tours' ? t('adminDash.bulkTourCategoryTitle', { count: total }) : t('adminDash.bulkCategoryTitle', { count: total })}
          </div>
          <button style={{ marginLeft: 'auto' }} onClick={onClose}>{t('adminDash.close')}</button>
        </div>
        {target === 'tours' && (
          <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginTop: 4 }}>{t('adminDash.bulkTourCategoryHelp')}</div>
        )}

        {/* Locked while Apply is in flight: its success closes the dialog, so a
            pick made meanwhile (not in the request) would be dropped. */}
        <LockFieldset locked={busy}>
        <div style={{ display: 'flex', gap: 10, marginTop: 12, flexWrap: 'wrap' }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1, minWidth: 160 }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.bulkCategoryKey')}</span>
            <Select size="sm" value={key} onChange={e => setKey(e.target.value)}>
              {keys.map(k => <option key={k} value={k}>{metaFieldLabel(t, k)}</option>)}
            </Select>
          </label>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1, minWidth: 160 }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.bulkCategoryMode')}</span>
            <Select size="sm" value={mode} onChange={e => setMode(e.target.value as Mode)}>
              <option value="add">{t('adminDash.bulkCategoryAdd')}</option>
              <option value="remove">{t('adminDash.bulkCategoryRemove')}</option>
            </Select>
          </label>
        </div>

        <div style={{ marginTop: 12 }}>
          <OptionMultiSelect
            label={metaFieldLabel(t, key)}
            k={key}
            options={displayOptions}
            selected={displaySelected}
            onChange={onMultiChange}
            t={t}
          />
        </div>
        </LockFieldset>

        <div style={{ display: 'flex', gap: 8, marginTop: 16, justifyContent: 'flex-end' }}>
          <Button size="sm" variant="secondary" onClick={onClose}>{t('adminDash.close')}</Button>
          <Button
            size="sm"
            variant={mode === 'remove' ? 'danger' : 'primary'}
            disabled={values.length === 0 || busy}
            onClick={() => onApply(key, values, mode)}
          >
            {t('adminDash.bulkCategoryApply')}
          </Button>
        </div>
      </div>
    </div>
  ), document.body)
}
