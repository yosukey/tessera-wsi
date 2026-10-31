// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The API module is the modal's only I/O; i18n and error formatting are real.
const api = vi.hoisted(() => ({
  adminUpdateSlide: vi.fn(),
  adminGetSlide: vi.fn(),
  adminFetchImageBlobUrl: vi.fn(),
  adminReplaceSlideWsi: vi.fn(),
  adminDownloadSlide: vi.fn(),
  adminGetStorage: vi.fn(),
}))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../lib/i18n'
import { NetworkError, RateLimitError } from '../../lib/api'
import type { Slide } from '../../lib/api'
import SlideEditModal from './SlideEditModal'

const SLIDE = {
  id: 7, public_id: 'p7', catalog_id: 'C-1', title: 'Liver', thumb_url: '', width: 1, height: 1,
  mpp_x: 0, mpp_y: 0, vendor: '', wsi_filename: 'a.svs', attributes: {},
  show_zoom_indicator: null, show_scale_bar: null, show_metadata_panel: null,
} as Slide

function mount(onSaved: () => Promise<void>) {
  return render(
    <I18nProvider>
      <SlideEditModal
        slide={SLIDE} metaOptions={{}} customMetaKeys={[]} globalOverlays={null}
        onClose={vi.fn()} onSaved={onSaved}
      />
    </I18nProvider>,
  )
}

// Save waits for the dialog's fresh preset fetch (adminGetSlide) to settle.
async function clickSave() {
  const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
  await waitFor(() => expect(save.disabled).toBe(false))
  fireEvent.click(save)
}

beforeEach(() => {
  localStorage.clear()
  api.adminUpdateSlide.mockResolvedValue(undefined)
  api.adminGetSlide.mockResolvedValue(SLIDE)
  api.adminGetStorage.mockResolvedValue({ total_bytes: 0, free_bytes: 0, max_upload_bytes: 8 * 1024 ** 3 })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe('SlideEditModal save', () => {
  it('reports success when the PATCH committed but the list refresh failed', async () => {
    const onSaved = vi.fn().mockRejectedValue(new RateLimitError())
    mount(onSaved)
    await clickSave()
    await screen.findByText('Saved')
    expect(api.adminUpdateSlide).toHaveBeenCalledTimes(1)
    expect(onSaved).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/Save failed/)).toBeNull()
  })

  it('still reports a failed PATCH as a failure', async () => {
    api.adminUpdateSlide.mockRejectedValue(new RateLimitError())
    const onSaved = vi.fn().mockResolvedValue(undefined)
    mount(onSaved)
    await clickSave()
    await waitFor(() => expect(screen.getByText(/Save failed/)).toBeTruthy())
    expect(onSaved).not.toHaveBeenCalled()
  })
})

describe('SlideEditModal image credit', () => {
  const payload = () => api.adminUpdateSlide.mock.calls[0][1] as Record<string, string>

  it('leaves the credit out of a save that did not touch it', async () => {
    mount(vi.fn().mockResolvedValue(undefined))
    await clickSave()
    await screen.findByText('Saved')
    expect(payload().credit_json).toBeUndefined()
  })

  it('sends all three credit fields as one trimmed object', async () => {
    mount(vi.fn().mockResolvedValue(undefined))
    fireEvent.change(screen.getByRole('combobox', { name: 'Credit' }), { target: { value: '  © Example Lab  ' } })
    fireEvent.change(screen.getByRole('combobox', { name: 'License' }), { target: { value: 'CC-BY-4.0' } })
    fireEvent.change(screen.getByRole('textbox', { name: 'Source URL' }), { target: { value: ' https://example.org/s ' } })
    await clickSave()
    await screen.findByText('Saved')
    expect(JSON.parse(payload().credit_json)).toEqual({
      credit_text: '© Example Lab', license_id: 'CC-BY-4.0', source_url: 'https://example.org/s',
    })
  })

  it('refuses a source URL that is not http(s) before sending', async () => {
    mount(vi.fn().mockResolvedValue(undefined))
    fireEvent.change(screen.getByRole('textbox', { name: 'Source URL' }), { target: { value: 'javascript:alert(1)' } })
    await clickSave()
    await screen.findByText(/Source URL: enter an address/)
    expect(api.adminUpdateSlide).not.toHaveBeenCalled()
  })

  it('refuses "https:///host", which the server reads as having no host', async () => {
    mount(vi.fn().mockResolvedValue(undefined))
    fireEvent.change(screen.getByRole('textbox', { name: 'Source URL' }), { target: { value: 'https:///example.org/' } })
    await clickSave()
    await screen.findByText(/Source URL: enter an address/)
    expect(api.adminUpdateSlide).not.toHaveBeenCalled()
  })

  it('reports an over-long source URL as a length error, as the server does', async () => {
    mount(vi.fn().mockResolvedValue(undefined))
    const url = 'https://example.org/' + 'a'.repeat(1100)
    fireEvent.change(screen.getByRole('textbox', { name: 'Source URL' }), { target: { value: url } })
    await clickSave()
    await screen.findByText('Source URL: enter within 1024 characters (currently 1120)')
    expect(api.adminUpdateSlide).not.toHaveBeenCalled()
  })

  it('does not let a stored credit it would refuse block an unrelated save', async () => {
    // 300 emoji: 300 characters to the server, 600 UTF-16 units to JS.
    const stored = { ...SLIDE, credit_text: '🔬'.repeat(300), source_url: 'https://example.org/\ufeff' } as Slide
    render(
      <I18nProvider>
        <SlideEditModal
          slide={stored} metaOptions={{}} customMetaKeys={[]} globalOverlays={null}
          onClose={vi.fn()} onSaved={vi.fn().mockResolvedValue(undefined)}
        />
      </I18nProvider>,
    )
    await clickSave()
    await screen.findByText('Saved')
    expect(payload().credit_json).toBeUndefined()
  })

  it('counts the credit length in characters, as the server does', async () => {
    mount(vi.fn().mockResolvedValue(undefined))
    fireEvent.change(screen.getByRole('combobox', { name: 'Credit' }), { target: { value: '🔬'.repeat(300) } })
    await clickSave()
    await screen.findByText('Saved')
    expect(JSON.parse(payload().credit_json).credit_text).toBe('🔬'.repeat(300))
  })

  it('previews the credit as the viewer will show it', () => {
    mount(vi.fn())
    fireEvent.change(screen.getByRole('combobox', { name: 'License' }), { target: { value: 'CC0-1.0' } })
    const link = screen.getByRole('link', { name: 'CC0 1.0' })
    expect(link.getAttribute('href')).toBe('https://creativecommons.org/publicdomain/zero/1.0/')
  })
})

describe('SlideEditModal metadata checks', () => {
  const payload = () => api.adminUpdateSlide.mock.calls[0][1] as Record<string, string>

  function mountWith(slide: Slide) {
    return render(
      <I18nProvider>
        <SlideEditModal
          slide={slide} metaOptions={{}} customMetaKeys={[]} globalOverlays={null}
          onClose={vi.fn()} onSaved={vi.fn().mockResolvedValue(undefined)}
        />
      </I18nProvider>,
    )
  }

  it('saves a title of 200 emoji (200 characters, though 400 UTF-16 units)', async () => {
    mountWith({ ...SLIDE, title: '🔬'.repeat(200) } as Slide)
    await clickSave()
    await screen.findByText('Saved')
    expect(payload().title).toBe('🔬'.repeat(200))
  })

  it('does not let a stored title it would refuse block an unrelated save', async () => {
    mountWith({ ...SLIDE, title: 'Imported\u0001title', attributes: { 'odd:key': ['v'] } } as Slide)
    await clickSave()
    await screen.findByText('Saved')
    expect(api.adminUpdateSlide).toHaveBeenCalledTimes(1)
  })

  it('still refuses an edited title with a control character', async () => {
    mountWith(SLIDE)
    fireEvent.change(screen.getByDisplayValue('Liver'), { target: { value: 'Liv\u0001er' } })
    await clickSave()
    await screen.findByText(/control characters/)
    expect(api.adminUpdateSlide).not.toHaveBeenCalled()
  })
})

describe('SlideEditModal WSI replace', () => {
  // Hold the replace open and hand the test its callbacks.
  async function replacing(file: File = new File(['x'], 'b.svs')) {
    const call = {} as { progress: (p: number) => void; sent?: () => void; fail: (e: unknown) => void }
    api.adminReplaceSlideWsi.mockImplementation(
      (_id: number, _f: File, onProgress: (p: number) => void, opts: { onSent?: () => void } = {}) => {
        call.progress = onProgress
        call.sent = opts.onSent
        return new Promise((_res, rej) => { call.fail = rej })
      },
    )
    // The modal renders into a portal, outside the render container.
    const input = document.querySelector('input[type=file][accept*=".svs"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [file] } })
    // The modal asks the server for its cap before sending.
    await waitFor(() => expect(api.adminReplaceSlideWsi).toHaveBeenCalled())
    return call
  }
  const sized = (name: string, size: number) => {
    const f = new File(['x'], name)
    Object.defineProperty(f, 'size', { value: size })
    return f
  }
  const pick = (file: File) => {
    const input = document.querySelector('input[type=file][accept*=".svs"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [file] } })
  }
  const verify = /Connection lost after the file finished uploading/

  it('does not ask to verify a connection lost while the file was still being sent', async () => {
    mount(vi.fn())
    const call = await replacing()
    act(() => call.progress(99))
    await screen.findByRole('button', { name: 'Uploading… 99%' })
    act(() => call.fail(new NetworkError()))
    await screen.findByRole('button', { name: 'Replace WSI file' })
    expect(screen.queryByText(verify)).toBeNull()
  })

  it('switches to processing once the whole file is sent, and then asks to verify a lost connection', async () => {
    mount(vi.fn())
    const call = await replacing()
    act(() => call.progress(99))
    act(() => call.sent?.())
    await screen.findByRole('button', { name: 'Processing on server…' })
    act(() => call.fail(new NetworkError()))
    await screen.findByText(verify)
  })

  it('refuses a file over the server cap without sending it, naming that cap', async () => {
    api.adminGetStorage.mockResolvedValue({ total_bytes: 0, free_bytes: 0, max_upload_bytes: 4 * 1024 ** 3 })
    mount(vi.fn())
    pick(sized('b.svs', 5 * 1024 ** 3))
    await screen.findByText('File size exceeds the limit (4 GB)')
    expect(api.adminReplaceSlideWsi).not.toHaveBeenCalled()
  })

  it('sends a file above the 8 GiB default when the server allows it', async () => {
    api.adminGetStorage.mockResolvedValue({ total_bytes: 0, free_bytes: 0, max_upload_bytes: 16 * 1024 ** 3 })
    mount(vi.fn())
    await replacing(sized('b.svs', 9 * 1024 ** 3))
  })

  it('leaves the decision to the server when its cap cannot be read', async () => {
    api.adminGetStorage.mockRejectedValue(new NetworkError())
    mount(vi.fn())
    const call = await replacing(sized('b.svs', 100 * 1024 ** 3))
    const { FileTooLargeError } = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
    act(() => call.fail(new FileTooLargeError()))
    await screen.findByText('File is larger than the server accepts.')
  })
})

describe('SlideEditModal a11y', () => {
  it('labels the dialog with its title', () => {
    mount(vi.fn())
    const dialog = screen.getByRole('dialog', { name: 'Edit metadata: 7' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
  })

  const tab = (shift = false) => fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Tab', shiftKey: shift })

  it('keeps focus inside, also in the image zoom, which returns it to its image', async () => {
    api.adminFetchImageBlobUrl.mockResolvedValue('blob:macro')
    render(
      <I18nProvider>
        <SlideEditModal
          slide={{ ...SLIDE, macro_url: '/api/admin/slides/7/macro' } as Slide}
          metaOptions={{}} customMetaKeys={[]} globalOverlays={null}
          onClose={vi.fn()} onSaved={vi.fn()}
        />
      </I18nProvider>,
    )
    const dialog = screen.getByRole('dialog', { name: 'Edit metadata: 7' })
    const first = document.activeElement
    expect(dialog.contains(first)).toBe(true)
    tab(true)
    expect(document.activeElement).not.toBe(first)
    expect(dialog.contains(document.activeElement)).toBe(true)
    tab()
    expect(document.activeElement).toBe(first)

    const image = await screen.findByRole('button', { name: 'Click to view at 1:1' })
    image.focus()
    fireEvent.click(image)
    const zoom = screen.getByRole('dialog', { name: 'Macro (slide overview)' })
    const close = zoom.querySelector('button')!
    expect(document.activeElement).toBe(close)
    tab()
    expect(document.activeElement).toBe(close)
    tab(true)
    expect(document.activeElement).toBe(close)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Macro (slide overview)' })).toBeNull()
    expect(document.activeElement).toBe(image)
  })
})

describe('SlideEditModal MPP across a WSI replace', () => {
  // A replace that the test finishes; the replaced slide has the new file's MPP.
  function holdReplace() {
    let finish: () => void = () => {}
    api.adminReplaceSlideWsi.mockReturnValue(new Promise<void>((r) => { finish = r }))
    api.adminGetSlide.mockResolvedValue({ ...SLIDE, mpp_x: 0.25, mpp_y: 0.25 })
    const input = document.querySelector('input[type=file][accept*=".svs"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'b.svs')] } })
    return () => finish()
  }
  const mppInput = () => document.querySelector('input[name="mpp_x"]') as HTMLInputElement

  it('locks the MPP fields while the replace runs, then shows the new file\'s MPP', async () => {
    // The replace ends by showing the new file's MPP, which would overwrite
    // a value typed meanwhile.
    mount(vi.fn().mockResolvedValue(undefined))
    expect(mppInput().closest('fieldset')!.disabled).toBe(false)
    const finish = holdReplace()
    await waitFor(() => expect(mppInput().closest('fieldset')!.disabled).toBe(true))
    await act(async () => finish())
    await waitFor(() => expect(mppInput().closest('fieldset')!.disabled).toBe(false))
    expect(mppInput().value).toBe('0.25')
  })

  it('locks the MPP fields and a second pick from the file pick on, its pre-checks included', async () => {
    // The replace first asks the server for its upload cap; a second file
    // picked meanwhile would start a second replace.
    api.adminGetStorage.mockReturnValue(new Promise(() => {}))
    mount(vi.fn().mockResolvedValue(undefined))
    const input = document.querySelector('input[type=file][accept*=".svs"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'b.svs')] } })
    await waitFor(() => expect(mppInput().closest('fieldset')!.disabled).toBe(true))
    expect((screen.getByRole('button', { name: 'Replace WSI file' }) as HTMLButtonElement).disabled).toBe(true)
    expect(api.adminReplaceSlideWsi).not.toHaveBeenCalled()
  })

  it('leaves MPP out of a save sent while a replace is out', async () => {
    // The replace resets MPP to the new file's; the two would undo each other.
    mount(vi.fn().mockResolvedValue(undefined))
    fireEvent.change(mppInput(), { target: { value: '0.5' } })
    holdReplace()
    await waitFor(() => expect(api.adminReplaceSlideWsi).toHaveBeenCalled())
    await clickSave()
    await waitFor(() => expect(api.adminUpdateSlide).toHaveBeenCalledTimes(1))
    expect(api.adminUpdateSlide.mock.calls[0][1]).not.toHaveProperty('mpp_x')
  })

  it("doesn't let a save in flight across the replace take the old MPP back as its baseline", async () => {
    // Otherwise the next save would write the new file's MPP as an override.
    mount(vi.fn().mockResolvedValue(undefined))
    let finishSave: () => void = () => {}
    api.adminUpdateSlide.mockReturnValueOnce(new Promise<void>((r) => { finishSave = r }))
    const finishReplace = holdReplace()
    await waitFor(() => expect(api.adminReplaceSlideWsi).toHaveBeenCalled())
    await clickSave()
    await waitFor(() => expect(api.adminUpdateSlide).toHaveBeenCalledTimes(1))
    await act(async () => finishReplace())
    await waitFor(() => expect(mppInput().value).toBe('0.25'))
    await act(async () => finishSave())
    await screen.findByText('Saved')
    await clickSave()
    await waitFor(() => expect(api.adminUpdateSlide).toHaveBeenCalledTimes(2))
    expect(api.adminUpdateSlide.mock.calls[1][1]).not.toHaveProperty('mpp_x')
  })
})

describe('SlideEditModal preset fetch', () => {
  it("holds Save until the slide's current preset has loaded", async () => {
    // Save sends the preset: before the fetch lands it would write the cached
    // (possibly older) one back.
    let finish: (v: Slide) => void = () => {}
    api.adminGetSlide.mockReturnValue(new Promise((r) => { finish = r }))
    mount(vi.fn().mockResolvedValue(undefined))
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    await act(async () => finish(SLIDE))
    expect(save.disabled).toBe(false)
  })

  it('frees Save when the fetch fails', async () => {
    api.adminGetSlide.mockRejectedValue(new RateLimitError())
    mount(vi.fn().mockResolvedValue(undefined))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false))
  })
})
