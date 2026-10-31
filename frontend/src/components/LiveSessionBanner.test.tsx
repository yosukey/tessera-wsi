// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { toBannerSessions } from './LiveSessionBanner'

describe("the index banner's slide names", () => {
  const entry = { sid: 's', slide_id: 7, slide_public_id: 'p7', slide_title: 'From discovery', slide_catalog_id: 'D-7' }

  it('come from the loaded listing when it holds the slide', () => {
    const [b] = toBannerSessions([entry], new Map([[7, { title: 'Listed', catalog_id: 'L-7' }]]))
    expect([b.slide_title, b.slide_catalog_id]).toEqual(['Listed', 'L-7'])
  })

  it("fall back to the discovery entry's labels for a slide past the loaded page", () => {
    const [b] = toBannerSessions([entry], new Map())
    expect([b.slide_title, b.slide_catalog_id]).toEqual(['From discovery', 'D-7'])
  })

  it('are null when neither has them', () => {
    const [b] = toBannerSessions([{ sid: 's', slide_id: 7, slide_public_id: 'p7' }], new Map())
    expect([b.slide_title, b.slide_catalog_id]).toEqual([null, null])
  })
})
