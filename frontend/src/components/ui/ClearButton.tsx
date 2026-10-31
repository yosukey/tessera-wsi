import React from 'react'

type Props = {
  onClick: () => void
  /** Accessible label / tooltip, e.g. "Clear search". */
  label: string
}

/**
 * Circle-with-x button for clearing an input's value. Designed to sit inside
 * an Input's `rightAdornment`: it sets `pointer-events: auto` to opt back in
 * (the adornment wrapper disables pointer events) and uses negative margins to
 * grow the tap target beyond the small glyph, which keeps it comfortable on
 * touch devices without shifting the surrounding layout.
 */
export function ClearButton({ onClick, label }: Props) {
  return (
    <button
      type="button"
      // Clear on pointer-down before the input loses focus, so the caret stays
      // put and the user can keep typing afterwards.
      onMouseDown={e => e.preventDefault()}
      onClick={onClick}
      title={label}
      aria-label={label}
      style={{
        // The adornment wrapper sets pointer-events: none; re-enable here so
        // the button stays clickable.
        pointerEvents: 'auto',
        background: 'none',
        border: 'none',
        cursor: 'pointer',
        color: 'var(--color-text-faint)',
        padding: 6,
        margin: -6,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        borderRadius: '50%',
        lineHeight: 0,
        transition: 'color var(--duration-fast)',
      }}
      onMouseEnter={e => (e.currentTarget.style.color = 'var(--color-text)')}
      onMouseLeave={e => (e.currentTarget.style.color = 'var(--color-text-faint)')}
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
        <circle cx="8" cy="8" r="7" fill="currentColor" opacity="0.16" />
        <path
          d="M5.5 5.5l5 5M10.5 5.5l-5 5"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
        />
      </svg>
    </button>
  )
}
