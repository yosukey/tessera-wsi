import React, { useEffect, useRef, useState } from 'react'
import {
  DEFAULT_IMAGE_ADJUST,
  IMAGE_ADJUST_RANGES,
  ImageAdjust,
  imageAdjustEqual,
} from '../lib/imageAdjust'
import { useI18n } from '../lib/i18n'
import { useInFlight } from '../hooks/useInFlight'
import AdjustSliderRow from './AdjustSliderRow'
import ImageAdjustIcon from './icons/ImageAdjustIcon'

type Props = {
  value: ImageAdjust
  onChange: (next: ImageAdjust) => void
  // The slide's preset (or identity when none). Reset returns here and the
  // "modified" dot lights up when the live value diverges from it.
  baseline?: ImageAdjust
  // When true, a manager can persist the current value as the slide's preset.
  canSavePreset?: boolean
  onSavePreset?: (value: ImageAdjust) => Promise<void>
}

export default function ImageAdjustControls({
  value,
  onChange,
  baseline = DEFAULT_IMAGE_ADJUST,
  canSavePreset = false,
  onSavePreset,
}: Props) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  // One preset save at a time. The sliders stay usable meanwhile (the save
  // only moves the baseline), so the latest value is read back when it lands.
  const saveRequest = useInFlight()
  const valueRef = useRef(value)
  valueRef.current = value
  const wrapRef = useRef<HTMLDivElement | null>(null)

  // Close on outside click and on Escape so the panel doesn't trap focus.
  // Use ``pointerdown`` rather than ``mousedown`` so iOS Safari fires for
  // taps on non-clickable areas — Safari only synthesises ``mousedown``
  // for elements it considers interactive, so a tap on the bare OSD canvas
  // outside the panel wouldn't close the panel.
  useEffect(() => {
    if (!open) return
    function onPointerDown(e: PointerEvent) {
      const el = wrapRef.current
      if (!el) return
      if (!el.contains(e.target as Node)) setOpen(false)
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // "Modified" relative to the slide's baseline preset — that's what the user
  // actually sees as the slide's normal look, so the reset affordance and the
  // toggle-button dot key off divergence from it rather than from identity.
  const atBaseline = imageAdjustEqual(value, baseline)
  // True while the live value diverges from the persisted preset (baseline)
  // and can be saved. After a save, ViewerPage folds the result back into the
  // slide so baseline == value again, and this flips back to false.
  const presetDirty = canSavePreset && !atBaseline

  function update(key: keyof ImageAdjust, n: number) {
    onChange({ ...value, [key]: n })
    // A save still out stays "saving" (and its button disabled).
    if (saveState === 'saved' || saveState === 'error') setSaveState('idle')
  }

  function handleSave() {
    if (!onSavePreset) return
    return saveRequest.run(async () => {
      const sent = value
      setSaveState('saving')
      try {
        await onSavePreset(sent)
        // "Saved" only if the sliders still show what was saved.
        setSaveState(imageAdjustEqual(valueRef.current, sent) ? 'saved' : 'idle')
      } catch {
        setSaveState('error')
      }
    })
  }

  return (
    <div
      ref={wrapRef}
      style={{
        position: 'absolute',
        bottom: 'calc(12px + env(safe-area-inset-bottom, 0px))',
        left: 'calc(12px + env(safe-area-inset-left, 0px))',
        zIndex: 20,
      }}
    >
      {open && (
        <div
          role="dialog"
          aria-label={t('viewer.adjust.title')}
          style={{
            position: 'absolute',
            bottom: 'calc(100% + 8px)',
            left: 0,
            width: 240,
            background: 'rgba(255, 255, 255, 0.94)',
            backdropFilter: 'blur(6px)',
            WebkitBackdropFilter: 'blur(6px)',
            border: '1px solid var(--color-border)',
            borderRadius: 'var(--radius-lg)',
            boxShadow: 'var(--shadow-md, 0 6px 18px rgba(0,0,0,0.12))',
            padding: 'var(--space-3, 12px) var(--space-3, 12px)',
            color: 'var(--color-text)',
          }}
        >
          <div
            style={{
              fontSize: 'var(--font-size-sm, 13px)',
              fontWeight: 700,
              letterSpacing: '0.01em',
              marginBottom: 'var(--space-2, 8px)',
            }}
          >
            {t('viewer.adjust.title')}
          </div>

          <AdjustSliderRow
            label={t('viewer.adjust.brightness')}
            value={value.brightness}
            range={IMAGE_ADJUST_RANGES.brightness}
            onChange={(n) => update('brightness', n)}
          />
          <AdjustSliderRow
            label={t('viewer.adjust.contrast')}
            value={value.contrast}
            range={IMAGE_ADJUST_RANGES.contrast}
            onChange={(n) => update('contrast', n)}
          />
          <AdjustSliderRow
            label={t('viewer.adjust.saturation')}
            value={value.saturation}
            range={IMAGE_ADJUST_RANGES.saturation}
            onChange={(n) => update('saturation', n)}
          />

          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginTop: 'var(--space-2, 8px)',
              gap: 8,
            }}
          >
            <span
              style={{
                fontSize: 'var(--font-size-xs, 11px)',
                color: 'var(--color-text-faint, #6b7280)',
              }}
            >
              {canSavePreset ? t('viewer.adjust.presetNote') : t('viewer.adjust.tempNote')}
            </span>
            <button
              type="button"
              disabled={atBaseline}
              onClick={() => onChange(baseline)}
              style={{
                background: atBaseline ? 'transparent' : 'var(--color-surface, #fff)',
                color: atBaseline ? 'var(--color-text-faint, #9ca3af)' : 'var(--color-text, #111)',
                border: `1px solid ${atBaseline ? 'var(--color-border, #e5e7eb)' : 'var(--color-border-strong, #d1d5db)'}`,
                borderRadius: 'var(--radius-md, 6px)',
                padding: '3px 10px',
                fontSize: 'var(--font-size-xs, 11px)',
                fontWeight: 600,
                cursor: atBaseline ? 'default' : 'pointer',
                letterSpacing: '0.02em',
              }}
            >
              {t('viewer.adjust.reset')}
            </button>
          </div>

          {canSavePreset && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                marginTop: 'var(--space-2, 8px)',
                paddingTop: 'var(--space-2, 8px)',
                borderTop: '1px solid var(--color-border, #e5e7eb)',
                gap: 8,
              }}
            >
              <span
                style={{
                  fontSize: 'var(--font-size-xs, 11px)',
                  color:
                    saveState === 'saved'
                      ? 'var(--color-success-fg, #15803d)'
                      : saveState === 'error'
                        ? 'var(--color-danger-fg, #b91c1c)'
                        : 'var(--color-text-faint, #6b7280)',
                }}
              >
                {saveState === 'saved'
                  ? t('viewer.adjust.saved')
                  : saveState === 'error'
                    ? t('viewer.adjust.saveError')
                    : ''}
              </span>
              <button
                type="button"
                disabled={saveRequest.busy || (!presetDirty && saveState !== 'error')}
                onClick={handleSave}
                style={{
                  background: 'var(--color-brand-600, #2563eb)',
                  color: '#fff',
                  border: 'none',
                  borderRadius: 'var(--radius-md, 6px)',
                  padding: '4px 12px',
                  fontSize: 'var(--font-size-xs, 11px)',
                  fontWeight: 700,
                  cursor: saveState === 'saving' || (!presetDirty && saveState !== 'error') ? 'default' : 'pointer',
                  opacity: saveState === 'saving' || (!presetDirty && saveState !== 'error') ? 0.5 : 1,
                  letterSpacing: '0.02em',
                }}
              >
                {saveState === 'saving' ? t('viewer.adjust.saving') : t('viewer.adjust.savePreset')}
              </button>
            </div>
          )}
        </div>
      )}

      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        title={t('viewer.adjust.toggle')}
        aria-label={t('viewer.adjust.toggle')}
        aria-expanded={open}
        style={{
          position: 'relative',
          background: 'rgba(255, 255, 255, 0.82)',
          border: 'none',
          borderRadius: 5,
          cursor: 'pointer',
          color: 'rgba(0, 0, 0, 0.85)',
          padding: '4px 8px',
          lineHeight: 1,
          display: 'flex',
          alignItems: 'center',
          pointerEvents: 'auto',
          boxShadow: 'var(--shadow-sm)',
        }}
      >
        <ImageAdjustIcon size={16} />
        {!atBaseline && (
          <span
            aria-hidden
            style={{
              position: 'absolute',
              top: 2,
              right: 2,
              width: 6,
              height: 6,
              borderRadius: '50%',
              background: 'var(--color-brand-600, #2563eb)',
              boxShadow: '0 0 0 1.5px rgba(255,255,255,0.9)',
            }}
          />
        )}
      </button>
    </div>
  )
}
