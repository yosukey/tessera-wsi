// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useUnsavedNavigationGuard } from './useUnsavedNavigationGuard'

const goBack = () => act(async () => { window.history.back() })

beforeEach(() => {
  window.history.pushState({}, '', '/admin')
  window.history.pushState({}, '', '/admin/tours/1')
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('useUnsavedNavigationGuard', () => {
  it('pushes nothing while the editor is clean, and Back leaves without asking', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const before = window.history.length
    renderHook(() => useUnsavedNavigationGuard(false, 'discard?'))
    expect(window.history.length).toBe(before)
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin'))
    expect(confirm).not.toHaveBeenCalled()
  })

  it('pushes one sentinel entry when edits become unsaved', () => {
    const before = window.history.length
    const { rerender } = renderHook(({ d }) => useUnsavedNavigationGuard(d, 'discard?'), {
      initialProps: { d: false },
    })
    rerender({ d: true })
    expect(window.history.length).toBe(before + 1)
    rerender({ d: false })
    rerender({ d: true })
    expect(window.history.length).toBe(before + 1)
  })

  it('Back + cancel keeps the user on the page and re-arms the guard', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderHook(() => useUnsavedNavigationGuard(true, 'discard?'))
    const len = window.history.length
    await goBack()
    await waitFor(() => expect(confirm).toHaveBeenCalledWith('discard?'))
    await waitFor(() => expect(window.history.length).toBe(len))
    expect(window.location.pathname).toBe('/admin/tours/1')

    // The guard is still armed: a second Back asks again.
    await goBack()
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2))
    expect(window.location.pathname).toBe('/admin/tours/1')
  })

  it('Back + confirm leaves the page in one go (no extra Back press)', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderHook(() => useUnsavedNavigationGuard(true, 'discard?'))
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin'))
    expect(confirm).toHaveBeenCalledTimes(1)
  })

  it('after the edits are saved, Back skips the sentinel without asking', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { rerender } = renderHook(({ d }) => useUnsavedNavigationGuard(d, 'discard?'), {
      initialProps: { d: true },
    })
    rerender({ d: false })
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin'))
    expect(confirm).not.toHaveBeenCalled()
  })

  it('remounting on the same URL does not leave a sentinel per mount behind', async () => {
    // Two dirty->clean cycles in two mounts (a tab switched away and back):
    // one Back press must still leave the page.
    for (let i = 0; i < 2; i++) {
      const { rerender, unmount } = renderHook(({ d }) => useUnsavedNavigationGuard(d, 'discard?'), {
        initialProps: { d: false },
      })
      rerender({ d: true })
      rerender({ d: false })
      unmount()
      // The unmount pops its own sentinel (async popstate).
      await waitFor(() => expect(window.history.state?.wsiNavGuard).toBeUndefined())
    }
    expect(window.location.pathname).toBe('/admin/tours/1')
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin'))
  })

  it('a sentinel re-armed after Back + cancel is popped on unmount too', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { rerender, unmount } = renderHook(({ d }) => useUnsavedNavigationGuard(d, 'discard?'), {
      initialProps: { d: true },
    })
    await goBack()
    await waitFor(() => expect(window.confirm).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(window.history.state?.wsiNavGuard).toBeTruthy()) // re-pushed
    rerender({ d: false }) // the upload finished
    unmount()
    await waitFor(() => expect(window.history.state?.wsiNavGuard).toBeUndefined())
    expect(window.location.pathname).toBe('/admin/tours/1')
    await goBack() // one press leaves: no sentinel is left behind
    await waitFor(() => expect(window.location.pathname).toBe('/admin'))
  })

  it('an in-app exit from a guarded page costs one Back press, not three', async () => {
    const { result, rerender, unmount } = renderHook(({ d }) => useUnsavedNavigationGuard(d, 'discard?'), {
      initialProps: { d: true },
    })
    rerender({ d: false }) // saved; the sentinel stays until the page is left
    act(() => result.current(() => window.history.pushState({}, '', '/admin/dashboard')))
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    unmount()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/tours/1'))
    expect(window.history.state?.wsiNavGuard).toBeUndefined()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin'))
  })

  it('an in-app exit that replaces the page drops it from history', async () => {
    // e.g. after deleting the tour the page edits: Back must not reopen it.
    const { result, unmount } = renderHook(() => useUnsavedNavigationGuard(true, 'discard?'))
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    act(() => result.current(() => window.history.replaceState({}, '', '/admin/dashboard')))
    await waitFor(() => expect(window.location.pathname).toBe('/admin/dashboard'))
    unmount()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin'))
    // The caller confirmed already; the exit itself never asks.
    expect(confirm).not.toHaveBeenCalled()
  })

  it('a second in-app exit before the first has stepped back replaces it', async () => {
    // A double click on a link after saving: one step back, the later exit.
    const { result, rerender, unmount } = renderHook(({ d }) => useUnsavedNavigationGuard(d, 'discard?'), {
      initialProps: { d: true },
    })
    rerender({ d: false })
    const first = vi.fn()
    // (jsdom drops a second traversal queued behind the first; browsers run
    // both, landing on the page before this one.)
    const back = vi.spyOn(window.history, 'back')
    act(() => {
      result.current(first)
      result.current(() => window.history.pushState({}, '', '/admin/account'))
    })
    expect(back).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(window.location.pathname).toBe('/admin/account'))
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(window.location.pathname).toBe('/admin/account')
    expect(first).not.toHaveBeenCalled()
    unmount()
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin/tours/1'))
    expect(window.history.state?.wsiNavGuard).toBeUndefined()
  })

  it('takes over a sentinel left in place by a reload', async () => {
    // The reloaded page stands on the sentinel the previous load pushed.
    window.history.pushState({ wsiNavGuard: 'g-before-reload' }, '')
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderHook(() => useUnsavedNavigationGuard(false, 'discard?'))
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin'))
    expect(confirm).not.toHaveBeenCalled()
  })

  it('guards with a taken-over sentinel once edits are unsaved again', async () => {
    window.history.pushState({ wsiNavGuard: 'g-before-reload-2' }, '')
    const before = window.history.length
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { rerender } = renderHook(({ d }) => useUnsavedNavigationGuard(d, 'discard?'), {
      initialProps: { d: false },
    })
    rerender({ d: true })
    expect(window.history.length).toBe(before) // no second sentinel
    await goBack()
    await waitFor(() => expect(window.location.pathname).toBe('/admin'))
    expect(confirm).toHaveBeenCalledTimes(1)
  })

  it("does not take over the sentinel of a guard of this page load that is stepping off it", async () => {
    // A remount on the same URL before the old guard's step back has landed.
    const old = renderHook(({ d }) => useUnsavedNavigationGuard(d, 'discard?'), {
      initialProps: { d: true },
    })
    old.rerender({ d: false })
    old.unmount()
    renderHook(() => useUnsavedNavigationGuard(false, 'discard?'))
    await waitFor(() => expect(window.history.state?.wsiNavGuard).toBeUndefined())
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(window.location.pathname).toBe('/admin/tours/1')
  })

  it('an in-app exit with no sentinel in place navigates at once', () => {
    const { result } = renderHook(() => useUnsavedNavigationGuard(false, 'discard?'))
    const go = vi.fn()
    result.current(go)
    expect(go).toHaveBeenCalledTimes(1)
  })

  it('unmounting after navigating elsewhere does not undo that navigation', async () => {
    const { rerender, unmount } = renderHook(({ d }) => useUnsavedNavigationGuard(d, 'discard?'), {
      initialProps: { d: false },
    })
    rerender({ d: true })
    rerender({ d: false })
    window.history.pushState({}, '', '/admin/dashboard') // an in-app link
    unmount()
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })
    expect(window.location.pathname).toBe('/admin/dashboard')
  })
})
