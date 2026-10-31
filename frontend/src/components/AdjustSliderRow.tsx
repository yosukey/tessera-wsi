import React, { useId } from 'react'

type Props = {
  label: string
  value: number
  range: { min: number; max: number; step: number }
  onChange: (n: number) => void
}

// A labelled range slider with a live numeric read-out. Shared by the in-viewer
// ImageAdjustControls panel and the dashboard SlideEditModal preset editor so
// both surfaces dial brightness/contrast/saturation with identical controls.
export default function AdjustSliderRow({ label, value, range, onChange }: Props) {
  // Ties the visible label to the slider so screen readers announce its name.
  const labelId = useId()
  return (
    <div style={{ marginBottom: 6 }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          fontSize: 'var(--font-size-xs, 11px)',
          color: 'var(--color-text-muted, #4b5563)',
          marginBottom: 2,
        }}
      >
        <span id={labelId}>{label}</span>
        <span style={{ fontVariantNumeric: 'tabular-nums', fontFamily: 'monospace' }}>
          {value.toFixed(2)}
        </span>
      </div>
      <input
        type="range"
        min={range.min}
        max={range.max}
        step={range.step}
        value={value}
        aria-labelledby={labelId}
        aria-valuetext={value.toFixed(2)}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{
          width: '100%',
          accentColor: 'var(--color-brand-600, #2563eb)',
          cursor: 'pointer',
        }}
      />
    </div>
  )
}
