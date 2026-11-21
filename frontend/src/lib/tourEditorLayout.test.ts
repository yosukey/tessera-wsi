import { describe, expect, it } from 'vitest'
import { TOUR_EDITOR_STACK_QUERY, TOUR_VIEWER_STACK_QUERY, tourEditorLayout } from './tourEditorLayout'
import { matchesViewportQuery } from './viewportBox'

// As a browser would evaluate them for a viewport of that size (the same
// evaluator the app uses while the layout viewport is stale).
function matches(query: string, width: number, height: number): boolean {
  const m = matchesViewportQuery(query, width, height)
  if (m === null) throw new Error(`cannot evaluate ${query}`)
  return m
}

// Upright CSS-px sizes; landscape is the same with the sides swapped.
const IPADS: Array<[string, number, number]> = [
  ['iPad mini', 744, 1133],
  ['iPad (10th gen)', 820, 1180],
  ['iPad Pro 11"', 834, 1194],
  ['iPad Pro 12.9"', 1024, 1366],
  ['iPad Pro 13"', 1032, 1376],
  // Display Zoom: More Space. No published figure; 1192 x 1590 is the
  // 12.9" as commonly quoted, and the 13" scaled by the same factor.
  ['iPad Pro 12.9" (More Space)', 1192, 1590],
  ['iPad Pro 13" (More Space, estimated)', 1202, 1603],
]

describe.each([
  ['editor', TOUR_EDITOR_STACK_QUERY],
  ['viewer', TOUR_VIEWER_STACK_QUERY],
])('the tour %s stack query', (_page, query) => {
  it.each(IPADS)('stacks on a portrait %s and goes side by side in landscape', (_name, w, h) => {
    expect(matches(query, w, h)).toBe(true)
    expect(matches(query, h, w)).toBe(false)
  })

  it('stacks on a phone held upright', () => {
    expect(matches(query, 390, 844)).toBe(true)
  })

  it('keeps desktop windows and wide portrait monitors side by side', () => {
    expect(matches(query, 1440, 900)).toBe(false)
    expect(matches(query, 1280, 720)).toBe(false)
    expect(matches(query, 1440, 2560)).toBe(false)
  })
})

describe('the stack queries below the portrait-tablet rule', () => {
  // The editor's 560px sidebar needs more room than the viewer's panel.
  it('stacks the editor on a narrow landscape window the viewer still fits beside', () => {
    expect(matches(TOUR_EDITOR_STACK_QUERY, 780, 500)).toBe(true)
    expect(matches(TOUR_VIEWER_STACK_QUERY, 780, 500)).toBe(false)
    expect(matches(TOUR_VIEWER_STACK_QUERY, 700, 400)).toBe(true)
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
