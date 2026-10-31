// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { STORAGE_KEYS } from '../storageKeys'

afterEach(() => {
  sessionStorage.clear()
  vi.restoreAllMocks()
  vi.resetModules()
})

describe('adminFetchImageBlobUrl', () => {
  async function load() {
    const { adminFetchImageBlobUrl } = await import('./admin')
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:img')
    sessionStorage.setItem(STORAGE_KEYS.adminToken, 'staff.jwt.token')
    return adminFetchImageBlobUrl
  }

  it('turns an image answer into a blob URL, sending the staff token', async () => {
    const fetchImage = await load()
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(new Uint8Array([137, 80, 78, 71]), { status: 200, headers: { 'content-type': 'image/png' } }),
    )
    await expect(fetchImage('/assets/tours/a/b.png')).resolves.toBe('blob:img')
    expect((fetchMock.mock.calls[0][1] as RequestInit).headers).toMatchObject({ Authorization: 'Bearer staff.jwt.token' })
  })

  it('does not buffer an answer that is not an image, whatever the request carried', async () => {
    const fetchImage = await load()
    const blob = vi.fn()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      headers: new Headers({ 'content-type': 'application/octet-stream' }),
      blob,
    } as unknown as Response)
    await expect(fetchImage('/api/admin/slides/12/download')).resolves.toBe('')
    expect(blob).not.toHaveBeenCalled()
  })

  it('answers "" for a missing image', async () => {
    const fetchImage = await load()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('nope', { status: 404 }))
    await expect(fetchImage('/api/admin/slides/1/macro')).resolves.toBe('')
  })
})
