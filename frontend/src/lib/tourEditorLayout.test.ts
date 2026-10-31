import { describe, expect, it } from 'vitest'
import { tourEditorLayout } from './tourEditorLayout'

describe('tourEditorLayout', () => {
  it('keeps the fixed 560px sidebar beside the slide on a wide screen', () => {
    const l = tourEditorLayout(false, '100vh')
    expect(l.container.flexDirection).toBe('row')
    expect(l.aside.width).toBe(560)
    expect(l.aside.flexShrink).toBe(0)
    expect(l.main.minWidth).toBe(0)
  })

  it('stacks the slide above a full-width sidebar on a narrow screen', () => {
    const l = tourEditorLayout(true, '600px')
    expect(l.container.flexDirection).toBe('column')
    expect(l.container.height).toBe('600px')
    expect(l.aside.width).toBeUndefined()
    expect(l.aside.flex).toBe('0 0 55%')
    expect(l.main.minHeight).toBe(0)
  })
})
