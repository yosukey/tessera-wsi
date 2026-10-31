// @vitest-environment jsdom
import React from 'react'
import { MemoryRouter } from 'react-router'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { I18nProvider } from '../../lib/i18n'
import type { TourIndexItem } from '../../lib/api'
import TourRow from './TourRow'
import TourTile from './TourTile'

afterEach(cleanup)

const ITEM: TourIndexItem = {
  id: 5, public_id: 't5', slide_public_id: 'p7', slide_id: 7, title: 'Lobule', description: '',
  is_published: true, attributes: { level: ['basic'], stain: ['PAS'] },
  slide: {
    id: 7, public_id: 'p7', catalog_id: 'C-7', title: 'Liver', thumb_url: '',
    image_adjust_brightness: null, image_adjust_contrast: null, image_adjust_saturation: null,
    attributes: { stain: ['HE'], organ: ['Liver'] },
  },
}

function mount(node: React.ReactNode) {
  render(<MemoryRouter><I18nProvider>{node}</I18nProvider></MemoryRouter>)
}

describe('Tours view cards', () => {
  it("show the card categories from the slide's metadata plus the tour's own", () => {
    mount(<TourTile tour={ITEM} slide={ITEM.slide} cardAttrs={['stain', 'level']} />)
    expect(screen.getByText('HE, PAS · basic')).toBeTruthy()
  })

  it('in the list view too', () => {
    mount(<TourRow tour={ITEM} slide={ITEM.slide} variant="flat" cardAttrs={['level']} />)
    expect(screen.getByText('basic')).toBeTruthy()
  })

  it('show no metadata line without card categories', () => {
    mount(<TourTile tour={ITEM} slide={ITEM.slide} />)
    expect(screen.queryByText(/basic/)).toBeNull()
  })
})
