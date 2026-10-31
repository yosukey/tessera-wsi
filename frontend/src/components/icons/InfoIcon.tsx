import React from 'react'

// The circled "i" of the About triggers (index header, viewer, tour viewer).
// Decorative: the button around it carries the label.
export default function InfoIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden>
      <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.4" />
      <circle cx="8" cy="5.5" r="0.9" fill="currentColor" />
      <path d="M8 7.5v3.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  )
}
