// @vitest-environment jsdom
import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { usePreventPinchZoom } from './usePreventPinchZoom'

// jsdom has no Touch constructor, so a touchmove carries a fake `touches` list
// of the given length — all the hook reads.
function touchMove(fingers: number): Event {
  const e = new Event('touchmove', { bubbles: true, cancelable: true })
  Object.defineProperty(e, 'touches', { value: { length: fingers } })
  return e
}

function setup() {
  const el = document.createElement('aside')
  const child = document.createElement('p')
  el.appendChild(child)
  const hook = renderHook(() => usePreventPinchZoom({ current: el }))
  return { child, ...hook }
}

describe('usePreventPinchZoom', () => {
  it('cancels a two-finger touch pinch that starts anywhere inside the element', () => {
    const { child } = setup()
    const e = touchMove(2)
    child.dispatchEvent(e)
    expect(e.defaultPrevented).toBe(true)
  })

  it('leaves single-finger touch moves alone so the panel still scrolls', () => {
    const { child } = setup()
    const e = touchMove(1)
    child.dispatchEvent(e)
    expect(e.defaultPrevented).toBe(false)
  })

  it("cancels WebKit's gesture events", () => {
    const { child } = setup()
    for (const type of ['gesturestart', 'gesturechange']) {
      const e = new Event(type, { bubbles: true, cancelable: true })
      child.dispatchEvent(e)
      expect(e.defaultPrevented).toBe(true)
    }
  })

  it('cancels a ctrl + wheel (trackpad pinch) but not a plain wheel scroll', () => {
    const { child } = setup()
    const pinch = new WheelEvent('wheel', { bubbles: true, cancelable: true, ctrlKey: true, deltaY: -10 })
    child.dispatchEvent(pinch)
    expect(pinch.defaultPrevented).toBe(true)
    const scroll = new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 10 })
    child.dispatchEvent(scroll)
    expect(scroll.defaultPrevented).toBe(false)
  })

  it('detaches its listeners on unmount', () => {
    const { child, unmount } = setup()
    unmount()
    const e = touchMove(2)
    child.dispatchEvent(e)
    expect(e.defaultPrevented).toBe(false)
  })

  it('does nothing without an element', () => {
    expect(() => renderHook(() => usePreventPinchZoom({ current: null }))).not.toThrow()
  })
})
