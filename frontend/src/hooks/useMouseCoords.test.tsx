// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const attached = vi.hoisted(
  () => [] as Array<{ onMove: (c: { x: number; y: number } | null) => void; detach: ReturnType<typeof vi.fn> }>,
)
vi.mock('../lib/osd', () => ({
  attachMouseCoordsTracker: (_v: unknown, onMove: any) => {
    const detach = vi.fn()
    attached.push({ onMove, detach })
    return detach
  },
}))

import { useMouseCoords } from './useMouseCoords'

const viewerRef = { current: {} as any }

beforeEach(() => { attached.length = 0 })

describe('useMouseCoords', () => {
  it('attaches no tracker while disabled, so mouse moves cannot re-render the owner', () => {
    const { result } = renderHook(() => useMouseCoords(viewerRef, false, 1))
    expect(attached).toHaveLength(0)
    expect(result.current).toBeNull()
  })

  it('attaches when enabled, reports coordinates, and detaches + clears when disabled', () => {
    const { result, rerender } = renderHook(
      ({ on }) => useMouseCoords(viewerRef, on, 1),
      { initialProps: { on: false } },
    )
    rerender({ on: true })
    expect(attached).toHaveLength(1)
    act(() => attached[0].onMove({ x: 3, y: 4 }))
    expect(result.current).toEqual({ x: 3, y: 4 })

    rerender({ on: false })
    expect(attached[0].detach).toHaveBeenCalled()
    expect(result.current).toBeNull()
  })

  it('re-attaches to a rebuilt viewer when the generation changes', () => {
    const { rerender } = renderHook(({ g }) => useMouseCoords(viewerRef, true, g), {
      initialProps: { g: 1 },
    })
    rerender({ g: 2 })
    expect(attached).toHaveLength(2)
    expect(attached[0].detach).toHaveBeenCalled()
  })

  it('does nothing without a viewer', () => {
    renderHook(() => useMouseCoords({ current: null }, true, 1))
    expect(attached).toHaveLength(0)
  })
})
