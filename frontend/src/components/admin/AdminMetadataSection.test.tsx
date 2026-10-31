// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminGetMetadataCounts: vi.fn(),
  adminGetMetadataOptionUsage: vi.fn(),
  adminDeleteMetadataOption: vi.fn(),
  adminGetMetadataKeyUsage: vi.fn(),
  adminDeleteMetadataKey: vi.fn(),
  adminAddMetadataOption: vi.fn(),
  adminRenameMetadataOption: vi.fn(),
}))
// Admins also see the metadata-display lists.
const role = vi.hoisted(() => ({ admin: false }))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api, isAdminFromToken: () => role.admin }
})

import { I18nProvider } from '../../lib/i18n'
import AdminMetadataSection from './AdminMetadataSection'

// Locked by its own `disabled` or by a disabled <fieldset> around it (which a
// browser honours, though the control's own `disabled` stays false).
const isLocked = (el: Element) => (el as HTMLInputElement).disabled || el.closest('fieldset[disabled]') !== null

afterEach(() => {
  role.admin = false
  cleanup()
  vi.resetAllMocks()
})

const refreshTours = vi.fn()

function mount(mdSavedSnapshot = '', extra: Record<string, unknown> = {}) {
  return render(
    <I18nProvider>
      <AdminMetadataSection
        metaOptions={{ stain: ['H&E', 'PAS'], organ: ['Liver'] }}
        setMetaOptions={vi.fn()}
        customMetaKeys={[]}
        refreshMetadataOptions={vi.fn()}
        refreshSlides={vi.fn().mockResolvedValue(undefined)}
        refreshTours={refreshTours}
        vsExtraAttrs={[]} setVsExtraAttrs={vi.fn()}
        vsIndexCardAttrs={[]} setVsIndexCardAttrs={vi.fn()}
        vsIndexFilterAttrs={[]} setVsIndexFilterAttrs={vi.fn()}
        mdSavedSnapshot={mdSavedSnapshot} setMdSavedSnapshot={vi.fn()}
        metaOptionsLoaded
        countsRefreshSignal={0}
        {...extra}
      />
    </I18nProvider>,
  )
}

describe('AdminMetadataSection usage counts', () => {
  it('shows a value and a category no slide uses as 0 (the endpoint omits them)', async () => {
    // Only used keys / values are listed: PAS and the whole organ category are absent.
    api.adminGetMetadataCounts.mockResolvedValue({
      keys: { stain: { total: 2, published: 1 } },
      values: { stain: { 'H&E': { total: 2, published: 1 } } },
      tour_keys: {},
      tour_values: {},
    })
    mount()
    const zero = await screen.findAllByTitle('Used by 0 slide(s) total (0 published)')
    const pills = zero.map((b) => b.parentElement?.textContent ?? '')
    expect(pills.some((p) => p.startsWith('PAS0'))).toBe(true)
    expect(pills.some((p) => p.startsWith('Liver0'))).toBe(true)
    // The organ category's own badge reads 0 too (as do lab and tags).
    expect(zero.filter((b) => b.textContent === '0 slide(s)')).toHaveLength(3)
    // A used value keeps its count.
    const used = screen.getAllByTitle('Used by 2 slide(s) total (1 published)')
    expect(used.map((b) => b.parentElement?.textContent ?? '').some((p) => p.startsWith('H&E2'))).toBe(true)
  })

  it('shows no badges until the counts have loaded', () => {
    api.adminGetMetadataCounts.mockReturnValue(new Promise(() => {}))
    mount()
    expect(screen.queryByTitle(/Used by/)).toBeNull()
  })
})

describe('AdminMetadataSection and tour metadata', () => {
  it('counts a value only tours carry as used, with its own tour badge', async () => {
    api.adminGetMetadataCounts.mockResolvedValue({
      keys: {},
      values: {},
      tour_keys: { stain: { total: 1, published: 0 } },
      tour_values: { stain: { PAS: { total: 1, published: 0 } } },
    })
    mount()
    const tourBadges = await screen.findAllByTitle('Set on 1 tour(s) as their own metadata (0 published)')
    expect(tourBadges.map((b) => b.textContent).sort()).toEqual(['0/1 tours', 'T1'])
    const pill = tourBadges.find((b) => b.textContent === 'T1')!.parentElement!
    expect(pill.textContent?.startsWith('PAS')).toBe(true)
    // Not styled as unused (danger), unlike a value nothing carries.
    expect(pill.getAttribute('style')).not.toContain('danger')
    const unused = (await screen.findAllByText('Liver'))[0].closest('span')!
    expect(unused.getAttribute('style')).toContain('danger')
  })

  it('names the tours in the delete confirmation and reloads them afterwards', async () => {
    api.adminGetMetadataCounts.mockResolvedValue({ keys: {}, values: {}, tour_keys: {}, tour_values: {} })
    api.adminGetMetadataOptionUsage.mockResolvedValue({
      key: 'stain', value: 'PAS', slide_count: 1, published_slide_count: 1,
      tour_count: 2, published_tour_count: 0, slides_fingerprint: 'fp',
    })
    api.adminDeleteMetadataOption.mockResolvedValue({
      ok: true, options_removed: 1, slide_attrs_removed: 1, slides_affected: 1,
      tour_attrs_removed: 2, tours_affected: 2,
    })
    refreshTours.mockResolvedValue(undefined)
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    mount()
    const pas = (await screen.findAllByText('PAS'))[0]
    const del = Array.from(pas.closest('span')!.querySelectorAll('button')).find((b) => b.textContent === '×')!
    fireEvent.click(del)
    await waitFor(() => expect(api.adminDeleteMetadataOption).toHaveBeenCalledWith('stain', 'PAS', 'fp'))
    expect(confirmSpy.mock.calls[0][0]).toContain('1 slide(s) and 2 tour(s)')
    await waitFor(() => expect(refreshTours).toHaveBeenCalled())
    confirmSpy.mockRestore()
  })

  it('words the confirmation for a value only tours carry', async () => {
    api.adminGetMetadataCounts.mockResolvedValue({ keys: {}, values: {}, tour_keys: {}, tour_values: {} })
    api.adminGetMetadataOptionUsage.mockResolvedValue({
      key: 'stain', value: 'PAS', slide_count: 0, published_slide_count: 0,
      tour_count: 3, published_tour_count: 1, slides_fingerprint: 'fp',
    })
    api.adminDeleteMetadataOption.mockResolvedValue({ ok: true, options_removed: 1, slide_attrs_removed: 0, slides_affected: 0, tours_affected: 3 })
    refreshTours.mockResolvedValue(undefined)
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    mount()
    const pas = (await screen.findAllByText('PAS'))[0]
    fireEvent.click(Array.from(pas.closest('span')!.querySelectorAll('button')).find((b) => b.textContent === '×')!)
    await waitFor(() => expect(confirmSpy).toHaveBeenCalled())
    expect(confirmSpy.mock.calls[0][0]).toBe('Warning: value "PAS" is set on 3 tour(s). Deleting it will remove this value from those tours. Delete anyway?')
    confirmSpy.mockRestore()
  })

  it('confirms before deleting a value nothing uses', async () => {
    api.adminGetMetadataCounts.mockResolvedValue({ keys: {}, values: {}, tour_keys: {}, tour_values: {} })
    api.adminGetMetadataOptionUsage.mockResolvedValue({
      key: 'stain', value: 'PAS', slide_count: 0, published_slide_count: 0,
      tour_count: 0, published_tour_count: 0, slides_fingerprint: 'fp',
    })
    api.adminDeleteMetadataOption.mockResolvedValue({ ok: true, options_removed: 1, slide_attrs_removed: 0, slides_affected: 0 })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    mount()
    const pas = (await screen.findAllByText('PAS'))[0]
    fireEvent.click(Array.from(pas.closest('span')!.querySelectorAll('button')).find((b) => b.textContent === '×')!)
    await waitFor(() => expect(api.adminDeleteMetadataOption).toHaveBeenCalledWith('stain', 'PAS', 'fp'))
    expect(confirmSpy.mock.calls[0][0]).toBe('Delete value "PAS" from "Stain"?')
    confirmSpy.mockRestore()
  })

  it('keeps an unused value when its delete confirmation is cancelled', async () => {
    api.adminGetMetadataCounts.mockResolvedValue({ keys: {}, values: {}, tour_keys: {}, tour_values: {} })
    api.adminGetMetadataOptionUsage.mockResolvedValue({
      key: 'stain', value: 'PAS', slide_count: 0, published_slide_count: 0,
      tour_count: 0, published_tour_count: 0, slides_fingerprint: 'fp',
    })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    mount()
    const pas = (await screen.findAllByText('PAS'))[0]
    fireEvent.click(Array.from(pas.closest('span')!.querySelectorAll('button')).find((b) => b.textContent === '×')!)
    await waitFor(() => expect(confirmSpy).toHaveBeenCalled())
    expect(api.adminDeleteMetadataOption).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('words the category confirmation for one only tours use', async () => {
    api.adminGetMetadataCounts.mockResolvedValue({ keys: {}, values: {}, tour_keys: {}, tour_values: {} })
    api.adminGetMetadataKeyUsage.mockResolvedValue({
      key: 'level', slide_count: 0, published_slide_count: 0,
      tour_count: 2, published_tour_count: 0, slides_fingerprint: 'fp',
    })
    api.adminDeleteMetadataKey.mockResolvedValue({ ok: true, options_removed: 1, slide_attrs_removed: 0, slides_affected: 0, tours_affected: 2 })
    refreshTours.mockResolvedValue(undefined)
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(
      <I18nProvider>
        <AdminMetadataSection
          metaOptions={{ stain: [], organ: [], level: ['basic'] }}
          setMetaOptions={vi.fn()}
          customMetaKeys={['level']}
          refreshMetadataOptions={vi.fn()}
          refreshSlides={vi.fn().mockResolvedValue(undefined)}
          refreshTours={refreshTours}
          vsExtraAttrs={[]} setVsExtraAttrs={vi.fn()}
          vsIndexCardAttrs={[]} setVsIndexCardAttrs={vi.fn()}
          vsIndexFilterAttrs={[]} setVsIndexFilterAttrs={vi.fn()}
          mdSavedSnapshot="" setMdSavedSnapshot={vi.fn()}
          metaOptionsLoaded
          countsRefreshSignal={0}
        />
      </I18nProvider>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Delete category' }))
    await waitFor(() => expect(confirmSpy).toHaveBeenCalled())
    expect(confirmSpy.mock.calls[0][0]).toBe('Warning: category "level" is used by 2 tour(s). Deleting it will remove this category from those tours. Delete anyway?')
    await waitFor(() => expect(refreshTours).toHaveBeenCalled())
    confirmSpy.mockRestore()
  })
})

describe('AdminMetadataSection while a change is in flight', () => {
  const NO_COUNTS = { keys: {}, values: {}, tour_keys: {}, tour_values: {} }

  it("locks a category's new-value box until its add lands", async () => {
    // The success clears the box, which would wipe text typed after Enter.
    api.adminGetMetadataCounts.mockResolvedValue(NO_COUNTS)
    let finish: (v: unknown) => void = () => {}
    api.adminAddMetadataOption.mockReturnValue(new Promise((r) => { finish = r }))
    mount()
    const box = screen.getAllByPlaceholderText('New value')[0] as HTMLInputElement
    fireEvent.change(box, { target: { value: 'Giemsa' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(isLocked(box)).toBe(true))
    expect(api.adminAddMetadataOption).toHaveBeenCalledWith('stain', 'Giemsa')
    finish({})
    await waitFor(() => expect(isLocked(box)).toBe(false))
    expect(box.value).toBe('')
  })

  it('locks the rename editor (and the other values\' rename) until the rename lands', async () => {
    // The success closes the editor, which would drop text typed after Enter.
    api.adminGetMetadataCounts.mockResolvedValue(NO_COUNTS)
    let finish: (v: unknown) => void = () => {}
    api.adminRenameMetadataOption.mockReturnValue(new Promise((r) => { finish = r }))
    mount()
    fireEvent.click(screen.getAllByRole('button', { name: 'Rename value' })[0])
    const box = screen.getByDisplayValue('H&E') as HTMLInputElement
    fireEvent.change(box, { target: { value: 'HE' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(box.disabled).toBe(true))
    expect(api.adminRenameMetadataOption).toHaveBeenCalledTimes(1)
    expect(api.adminRenameMetadataOption).toHaveBeenCalledWith('stain', 'H&E', 'HE')
    for (const b of screen.getAllByRole('button', { name: 'Rename value' })) expect((b as HTMLButtonElement).disabled).toBe(true)
    // A repeat Enter doesn't send it again.
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(api.adminRenameMetadataOption).toHaveBeenCalledTimes(1)
    finish({ slides_affected: 0 })
    await waitFor(() => expect(screen.queryByDisplayValue('HE')).toBeNull())
  })

  it("holds deletes, renames and adds while a value's delete is out", async () => {
    // A second click would send a second DELETE, and a rename or an add of
    // what is being deleted would race it.
    api.adminGetMetadataCounts.mockResolvedValue(NO_COUNTS)
    api.adminGetMetadataOptionUsage.mockReturnValue(new Promise(() => {}))
    mount()
    const removes = await screen.findAllByRole('button', { name: '×' }) as HTMLButtonElement[]
    fireEvent.click(removes[0])
    await waitFor(() => expect(removes[0].disabled).toBe(true))
    expect(removes.every((b) => b.disabled)).toBe(true)
    expect((screen.getAllByRole('button', { name: 'Rename value' }) as HTMLButtonElement[]).every((b) => b.disabled)).toBe(true)
    expect((screen.getAllByPlaceholderText('New value') as HTMLInputElement[]).every(isLocked)).toBe(true)
    fireEvent.click(removes[1])
    expect(api.adminGetMetadataOptionUsage).toHaveBeenCalledTimes(1)
  })
})

describe('AdminMetadataSection display lists', () => {
  it('locks the lists until they have loaded (with the viewer settings)', async () => {
    // A change made before the load lands would be overwritten by it.
    role.admin = true
    api.adminGetMetadataCounts.mockResolvedValue({ keys: {}, values: {}, tour_keys: {}, tour_values: {} })
    const { unmount } = mount('')
    const list = (await screen.findByText('Attributes shown in the viewer info panel')).closest('fieldset')!
    expect(list.disabled).toBe(true)
    unmount()
    mount('{"loaded":true}')
    expect((await screen.findByText('Attributes shown in the viewer info panel')).closest('fieldset')!.disabled).toBe(false)
  })
})

describe('AdminMetadataSection more in-flight cases', () => {
  const NO_COUNTS = { keys: {}, values: {}, tour_keys: {}, tour_values: {} }

  it('locks a rename editor already open while a delete is out', async () => {
    // Otherwise its Enter would send a rename racing the delete.
    api.adminGetMetadataCounts.mockResolvedValue(NO_COUNTS)
    api.adminGetMetadataOptionUsage.mockReturnValue(new Promise(() => {}))
    mount()
    fireEvent.click((await screen.findAllByRole('button', { name: 'Rename value' }))[0])
    const editor = screen.getByDisplayValue('H&E') as HTMLInputElement
    fireEvent.click(screen.getAllByRole('button', { name: '×' })[0])
    await waitFor(() => expect(editor.disabled).toBe(true))
    fireEvent.change(editor, { target: { value: 'HE' } })
    fireEvent.keyDown(editor, { key: 'Enter' })
    expect(api.adminRenameMetadataOption).not.toHaveBeenCalled()
    expect((screen.getByPlaceholderText('category key (e.g. diagnosis)') as HTMLInputElement).disabled).toBe(true)
  })

  it('says why the display lists are locked when their load failed, with a retry', async () => {
    role.admin = true
    api.adminGetMetadataCounts.mockResolvedValue(NO_COUNTS)
    const retry = vi.fn()
    mount('', { displayLoadError: true, onRetryDisplayLoad: retry })
    const alert = await screen.findByText(/Failed to load viewer settings/)
    fireEvent.click(within(alert.parentElement!).getByRole('button', { name: 'Retry' }))
    expect(retry).toHaveBeenCalled()
  })
})
