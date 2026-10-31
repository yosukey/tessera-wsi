// @vitest-environment jsdom
import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { Slide } from '../lib/api'
import { I18nProvider } from '../lib/i18n'
import SlideInfoPanel from './SlideInfoPanel'

afterEach(cleanup)

// The panel's logo alt text is localised, so it renders inside the provider the app wraps every page in.
const renderPanel = (ui: React.ReactElement) => render(<I18nProvider>{ui}</I18nProvider>)

const noSettings = { extra_attrs: [], logo_url: '', copyright_msg: '' }

describe('SlideInfoPanel', () => {
  it('renders nothing while there is nothing to show (slide loading or failed)', () => {
    expect(renderPanel(<SlideInfoPanel slide={null} settings={noSettings} />).container.innerHTML).toBe('')
    const bare = { catalog_id: '', title: '', attributes: {} } as unknown as Slide
    expect(renderPanel(<SlideInfoPanel slide={bare} settings={noSettings} />).container.innerHTML).toBe('')
  })

  it('shows the slide fields, the selected extra attributes, logo and copyright', () => {
    const slide = { catalog_id: 'K-12', title: 'Kidney', attributes: { stain: ['HE', 'PAS'], organ: ['kidney'] } } as unknown as Slide
    renderPanel(
      <SlideInfoPanel
        slide={slide}
        settings={{ extra_attrs: ['catalog_id', 'stain'], logo_url: '/assets/logo.png', copyright_msg: '© Lab' }}
      />,
    )
    expect(screen.getByText('K-12')).toBeTruthy()
    expect(screen.getByText('Kidney')).toBeTruthy()
    expect(screen.getByText('HE, PAS')).toBeTruthy()
    expect(screen.queryByText('kidney')).toBeNull()
    expect(screen.getByRole('img', { name: 'Logo' }).getAttribute('src')).toBe('/assets/logo.png')
    expect(screen.getByText('© Lab')).toBeTruthy()
  })

  it("adds a tour's own metadata to the slide's in the tour viewer", () => {
    const slide = { catalog_id: 'K-12', title: 'Kidney', attributes: { stain: ['HE'] } } as unknown as Slide
    renderPanel(
      <SlideInfoPanel
        slide={slide}
        settings={{ ...noSettings, extra_attrs: ['stain', 'level'] }}
        tourAttributes={{ stain: ['PAS', 'HE'], level: ['basic'] }}
      />,
    )
    expect(screen.getByText('HE, PAS / basic')).toBeTruthy()
  })

  it('still shows the site copyright before the slide has loaded', () => {
    renderPanel(<SlideInfoPanel slide={null} settings={{ ...noSettings, copyright_msg: '© Lab' }} />)
    expect(screen.getByText('© Lab')).toBeTruthy()
  })
})
