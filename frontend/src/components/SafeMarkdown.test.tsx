// @vitest-environment jsdom
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render as rtlRender, waitFor } from '@testing-library/react'

vi.mock('../lib/api', () => ({
  adminFetchImageBlobUrl: vi.fn(),
}))

import * as api from '../lib/api'
import SafeMarkdown from './SafeMarkdown'
import { I18nProvider } from '../lib/i18n'

const wrapper = ({ children }: { children: React.ReactNode }) => <I18nProvider>{children}</I18nProvider>
const render = (ui: React.ReactElement) => rtlRender(ui, { wrapper })

const fetchBlob = api.adminFetchImageBlobUrl as unknown as ReturnType<typeof vi.fn>

describe('SafeMarkdown images', () => {
  beforeEach(() => {
    fetchBlob.mockReset()
    URL.revokeObjectURL = vi.fn()
  })
  afterEach(() => cleanup())

  it('loads a plain <img src> outside the admin editor', () => {
    const { container } = render(<SafeMarkdown markdown="![a](/assets/tours/p1/a.png)" />)
    expect(container.querySelector('img')?.getAttribute('src')).toBe('/assets/tours/p1/a.png')
    expect(fetchBlob).not.toHaveBeenCalled()
  })

  it('fetches a local asset with the admin bearer in the admin editor', async () => {
    // Under the ldap / google_oauth gate a plain <img src> 401s once the
    // admin has no (or an expired) app-access cookie.
    fetchBlob.mockResolvedValue('blob:abc')
    const { container } = render(<SafeMarkdown markdown="![a](/assets/tours/p1/a.png)" adminImages />)
    await waitFor(() => expect(container.querySelector('img')?.getAttribute('src')).toBe('blob:abc'))
    expect(fetchBlob).toHaveBeenCalledWith('/assets/tours/p1/a.png', expect.anything())
  })

  it('falls back to the plain URL when the admin fetch fails', async () => {
    fetchBlob.mockResolvedValue('')
    const { container } = render(<SafeMarkdown markdown="![a](/assets/tours/p1/a.png)" adminImages />)
    await waitFor(() => expect(container.querySelector('img')?.getAttribute('src')).toBe('/assets/tours/p1/a.png'))
  })

  it('does not send the admin bearer to a path that only starts with /assets/', () => {
    // "/assets/../api/admin/slides/12/download" is requested as
    // /api/admin/slides/12/download: a step description by any staff role must
    // not make the admin's editor fetch it with the admin's credential.
    const { container } = render(
      <SafeMarkdown markdown="![a](/assets/../api/admin/slides/12/download)" adminImages />,
    )
    expect(fetchBlob).not.toHaveBeenCalled()
    // Rendered as an ordinary <img>, which carries no Authorization header.
    expect(container.querySelector('img')?.getAttribute('src')).toBe('/assets/../api/admin/slides/12/download')
  })

  it('leaves external images alone in the admin editor', () => {
    const { container } = render(<SafeMarkdown markdown="![a](https://example.com/a.png)" adminImages />)
    expect(container.querySelector('img')?.getAttribute('src')).toBe('https://example.com/a.png')
    expect(fetchBlob).not.toHaveBeenCalled()
  })

  it('keeps the image mounted while the text around it is edited', async () => {
    fetchBlob.mockResolvedValue('blob:abc')
    const md = '![a](/assets/tours/p1/a.png)\n\nhello'
    const { container, rerender } = render(<SafeMarkdown markdown={md} adminImages />)
    await waitFor(() => expect(container.querySelector('img')?.getAttribute('src')).toBe('blob:abc'))
    rerender(<SafeMarkdown markdown={`${md} world`} adminImages />)
    expect(container.querySelector('img')?.getAttribute('src')).toBe('blob:abc')
    expect(fetchBlob).toHaveBeenCalledTimes(1)
  })
})
