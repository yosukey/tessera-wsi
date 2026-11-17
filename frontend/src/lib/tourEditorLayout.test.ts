import { describe, expect, it } from 'vitest'
import { PORTRAIT_TABLET_QUERY, TOUR_EDITOR_STACK_QUERY, tourEditorLayout } from './tourEditorLayout'

describe('TOUR_EDITOR_STACK_QUERY', () => {
  // So rotating an iPad (744–1032px wide upright) switches the layout.
  it('stacks on a narrow window and on a portrait tablet', () => {
    expect(TOUR_EDITOR_STACK_QUERY).toBe(`(max-width: 800px), ${PORTRAIT_TABLET_QUERY}`)
    expect(PORTRAIT_TABLET_QUERY).toContain('(orientation: portrait)')
    const maxWidth = Number(/max-width: (\d+)px/.exec(PORTRAIT_TABLET_QUERY)?.[1])
    // Every upright iPad, up to the 13" Pro.
    expect(maxWidth).toBeGreaterThanOrEqual(1032)
  })
})

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
