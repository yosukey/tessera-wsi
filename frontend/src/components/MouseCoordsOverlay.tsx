import React from 'react'

// The pointer's level-0 image coordinates, shown above the debug toggle while
// the viewers are in debug mode (and in the tour editor while placing a pin or
// a shape).
export default function MouseCoordsOverlay({ coords }: { coords: { x: number; y: number } | null }) {
  if (!coords) return null
  return (
    <div style={{
      position: 'absolute', bottom: 'calc(92px + env(safe-area-inset-bottom, 0px))', left: 'calc(12px + env(safe-area-inset-left, 0px))', zIndex: 10,
      background: 'rgba(0, 0, 0, 0.65)',
      color: '#e5e7eb',
      padding: '3px 8px',
      borderRadius: 'var(--radius-md, 6px)',
      fontSize: 11,
      fontFamily: 'monospace',
      pointerEvents: 'none',
      userSelect: 'none',
      letterSpacing: '0.03em',
    }}>
      x: {Math.round(coords.x)}&nbsp;&nbsp;y: {Math.round(coords.y)}
    </div>
  )
}
