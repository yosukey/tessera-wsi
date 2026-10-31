// @vitest-environment jsdom
import React, { useEffect, useLayoutEffect } from 'react'
import { describe, expect, it } from 'vitest'
import { render, renderHook } from '@testing-library/react'
import { useStableCallback } from './useStableCallback'

describe('useStableCallback', () => {
  it('keeps one identity but always calls the latest function', () => {
    const { result, rerender } = renderHook(({ n }) => useStableCallback(() => n), { initialProps: { n: 1 } })
    const first = result.current
    expect(first()).toBe(1)
    rerender({ n: 2 })
    expect(result.current).toBe(first)
    expect(first()).toBe(2)
  })

  it('is already current when a child effect runs in the commit that changed it', () => {
    const seen: string[] = []
    function Child({ cb, n }: { cb: () => number; n: number }) {
      useLayoutEffect(() => { seen.push(`layout:${cb()}`) }, [n, cb])
      useEffect(() => { seen.push(`effect:${cb()}`) }, [n, cb])
      return null
    }
    function Parent({ n }: { n: number }) {
      const cb = useStableCallback(() => n)
      return React.createElement(Child, { cb, n })
    }
    const view = render(React.createElement(Parent, { n: 1 }))
    seen.length = 0
    view.rerender(React.createElement(Parent, { n: 2 }))
    // Children's effects run before the parent's own: a ref refreshed in the
    // parent's (layout) effect would still hand them the previous closure.
    expect(seen).toEqual(['layout:2', 'effect:2'])
  })
})
