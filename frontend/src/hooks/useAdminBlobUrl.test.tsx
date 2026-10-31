// @vitest-environment jsdom
import { cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/api', () => ({ adminFetchImageBlobUrl: vi.fn() }))

import { adminFetchImageBlobUrl } from '../lib/api'
import { cssImageUrl, useAdminBlobUrl } from './useAdminBlobUrl'

const fetchBlob = vi.mocked(adminFetchImageBlobUrl)
const revoke = vi.fn()

beforeEach(() => {
  fetchBlob.mockReset()
  revoke.mockReset()
  URL.revokeObjectURL = revoke
})
afterEach(() => cleanup())

describe('useAdminBlobUrl', () => {
  it('loads the image with the admin bearer and revokes it when the URL changes', async () => {
    fetchBlob.mockImplementation(async (url: string) => `blob:${url}`)
    const { result, rerender, unmount } = renderHook(({ url }) => useAdminBlobUrl(url), {
      initialProps: { url: '/api/viewer/logo?v=1' },
    })
    await waitFor(() => expect(result.current).toBe('blob:/api/viewer/logo?v=1'))
    expect(fetchBlob).toHaveBeenCalledWith('/api/viewer/logo?v=1', expect.anything())

    rerender({ url: '/api/viewer/logo?v=2' })
    await waitFor(() => expect(result.current).toBe('blob:/api/viewer/logo?v=2'))
    expect(revoke).toHaveBeenCalledWith('blob:/api/viewer/logo?v=1')

    unmount()
    expect(revoke).toHaveBeenCalledWith('blob:/api/viewer/logo?v=2')
  })

  it('fetches nothing for an empty URL', () => {
    const { result } = renderHook(() => useAdminBlobUrl(''))
    expect(result.current).toBe('')
    expect(fetchBlob).not.toHaveBeenCalled()
  })

  it('revokes a blob that arrives after the component is gone', async () => {
    let resolve: (u: string) => void = () => {}
    fetchBlob.mockImplementation(() => new Promise<string>((r) => { resolve = r }))
    const { unmount } = renderHook(() => useAdminBlobUrl('/api/admin/viewer/seasonal-backgrounds/1/image?v=a'))
    unmount()
    resolve('blob:late')
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:late'))
  })

  it('cssImageUrl paints nothing until the blob is there', () => {
    expect(cssImageUrl('')).toBe('none')
    expect(cssImageUrl('blob:x')).toBe('url("blob:x")')
  })
})
