import React from 'react'

type Props = {
  size?: number
  // SVG title for standalone (non-decorative) use. When omitted the icon is
  // marked aria-hidden, matching how it's used inside a labelled button.
  title?: string
}

// The sliders glyph that denotes image adjustment (brightness/contrast/
// saturation). Shared by the viewer's ImageAdjustControls toggle and the
// admin slide-list "preset active" overlay so the metaphor stays consistent.
export default function ImageAdjustIcon({ size = 16, title }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : true}
    >
      {title && <title>{title}</title>}
      <path d="M2 4h7" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <path d="M12 4h2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="10.5" cy="4" r="1.5" fill="currentColor" />
      <path d="M2 8h2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <path d="M7 8h7" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="5.5" cy="8" r="1.5" fill="currentColor" />
      <path d="M2 12h9" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <path d="M14 12h0" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      <circle cx="12.5" cy="12" r="1.5" fill="currentColor" />
    </svg>
  )
}
