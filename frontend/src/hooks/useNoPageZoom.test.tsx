// @vitest-environment jsdom
import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { NO_PAGE_ZOOM_CLASS, useNoPageZoom } from './useNoPageZoom'

const ORIGINAL = 'width=device-width, initial-scale=1.0, viewport-fit=cover'

// jsdom has no Touch constructor, so a touchmove carries a fake `touches` list
// of the given length (the hook registers no touchmove listener; the test
// checks such events pass untouched).
function touchMove(fingers: number): Event {
  const e = new Event('touchmove', { bubbles: true, cancelable: true })
  Object.defineProperty(e, 'touches', { value: { length: fingers } })
  return e
}

function viewport(): HTMLMetaElement {
  return document.querySelector('meta[name="viewport"]') as HTMLMetaElement
}

describe('useNoPageZoom', () => {
  beforeEach(() => {
    const meta = document.createElement('meta')
    meta.name = 'viewport'
    meta.content = ORIGINAL
    document.head.appendChild(meta)
  })
  afterEach(() => {
    viewport()?.remove()
  })

  it('caps the viewport scale while mounted and restores it on unmount', () => {
    const { unmount } = renderHook(() => useNoPageZoom())
    expect(viewport().content).toBe(`${ORIGINAL}, maximum-scale=1, user-scalable=no`)
    unmount()
    expect(viewport().content).toBe(ORIGINAL)
  })

  it('sets the touch-action class on <html> only while mounted', () => {
    const { unmount } = renderHook(() => useNoPageZoom())
    expect(document.documentElement.classList.contains(NO_PAGE_ZOOM_CLASS)).toBe(true)
    unmount()
    expect(document.documentElement.classList.contains(NO_PAGE_ZOOM_CLASS)).toBe(false)
  })

  it('cancels WebKit pinch gestures anywhere in the document on a touch device', () => {
    Object.defineProperty(navigator, 'maxTouchPoints', { value: 5, configurable: true })
    const { unmount } = renderHook(() => useNoPageZoom())
    const portal = document.createElement('div')
    document.body.appendChild(portal)
    const pinch = new Event('gesturestart', { bubbles: true, cancelable: true })
    portal.dispatchEvent(pinch)
    expect(pinch.defaultPrevented).toBe(true)
    // No touchmove listener: one-finger scrolls (and two) pass untouched.
    const move = touchMove(2)
    portal.dispatchEvent(move)
    expect(move.defaultPrevented).toBe(false)
    unmount()
    const after = new Event('gesturestart', { bubbles: true, cancelable: true })
    portal.dispatchEvent(after)
    expect(after.defaultPrevented).toBe(false)
    portal.remove()
    Object.defineProperty(navigator, 'maxTouchPoints', { value: 0, configurable: true })
  })

  it('leaves ctrl + wheel to the desktop browser', () => {
    const { unmount } = renderHook(() => useNoPageZoom())
    const e = new WheelEvent('wheel', { bubbles: true, cancelable: true, ctrlKey: true, deltaY: -10 })
    document.body.dispatchEvent(e)
    expect(e.defaultPrevented).toBe(false)
    unmount()
  })
})
