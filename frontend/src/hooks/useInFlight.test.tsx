// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LockFieldset } from '../components/ui'
import { useInFlight, useInFlightKey } from './useInFlight'

afterEach(cleanup)

function deferred<T = void>() {
  let resolve: (v: T) => void = () => {}
  let reject: (e: unknown) => void = () => {}
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('useInFlight', () => {
  it('is busy while a run is out, and refuses a second one meanwhile', async () => {
    const { result } = renderHook(() => useInFlight())
    expect(result.current.busy).toBe(false)
    const first = deferred<string>()
    const second = vi.fn(async () => 'second')
    let firstResult: Promise<string | undefined> = Promise.resolve(undefined)
    let secondResult: Promise<string | undefined> = Promise.resolve(undefined)
    act(() => {
      firstResult = result.current.run(() => first.promise)
      // Same tick, before `busy` has re-rendered: still refused.
      secondResult = result.current.run(second)
    })
    expect(result.current.busy).toBe(true)
    expect(second).not.toHaveBeenCalled()
    await expect(secondResult).resolves.toBeUndefined()
    await act(async () => first.resolve('first'))
    await expect(firstResult).resolves.toBe('first')
    expect(result.current.busy).toBe(false)
    // Free again once it has landed.
    await act(async () => { await result.current.run(second) })
    expect(second).toHaveBeenCalledTimes(1)
  })

  it('passes an error on and is free again after it', async () => {
    const { result } = renderHook(() => useInFlight())
    const failing = deferred()
    let out: Promise<unknown> = Promise.resolve()
    act(() => { out = result.current.run(() => failing.promise) })
    await act(async () => {
      failing.reject(new Error('boom'))
      await expect(out).rejects.toThrow('boom')
    })
    expect(result.current.busy).toBe(false)
  })
})

describe('useInFlightKey', () => {
  it("says whose run is out, and refuses another row's meanwhile", async () => {
    const { result } = renderHook(() => useInFlightKey<number>())
    expect(result.current.key).toBeNull()
    const row1 = deferred()
    const row2 = vi.fn(async () => {})
    act(() => {
      void result.current.run(1, () => row1.promise)
      void result.current.run(2, row2)
    })
    expect(result.current.key).toBe(1)
    expect(row2).not.toHaveBeenCalled()
    await act(async () => row1.resolve())
    expect(result.current.key).toBeNull()
  })
})

describe('LockFieldset', () => {
  it('disables what it holds while locked, without a frame of its own', () => {
    const { rerender } = render(
      <LockFieldset locked={false} style={{ display: 'grid' }}><input aria-label="name" /></LockFieldset>,
    )
    const fieldset = screen.getByLabelText('name').closest('fieldset')!
    expect(fieldset.disabled).toBe(false)
    expect(fieldset.getAttribute('style')).toMatch(/border: 0/)
    expect(fieldset.style.padding).toBe('0px')
    expect(fieldset.style.display).toBe('grid')
    rerender(<LockFieldset locked><input aria-label="name" /></LockFieldset>)
    expect(fieldset.disabled).toBe(true)
  })

  it('gives focus back to the control that had it when the lock began', () => {
    // A disabled control drops its focus; a wrong password should be
    // retyped at once, without clicking back into the field.
    const { rerender } = render(<><LockFieldset locked={false}><input aria-label="pw" /></LockFieldset><input aria-label="other" /></>)
    const pw = screen.getByLabelText('pw') as HTMLInputElement
    pw.focus()
    rerender(<><LockFieldset locked><input aria-label="pw" /></LockFieldset><input aria-label="other" /></>)
    // A browser moves focus off a control it disables (jsdom keeps it).
    Object.defineProperty(document, 'activeElement', { configurable: true, get: () => document.body })
    const focus = vi.spyOn(pw, 'focus')
    try {
      rerender(<><LockFieldset locked={false}><input aria-label="pw" /></LockFieldset><input aria-label="other" /></>)
    } finally {
      delete (document as unknown as Record<string, unknown>).activeElement
    }
    // Without scrolling the page back to it.
    expect(focus).toHaveBeenCalledWith({ preventScroll: true })
  })

  it("doesn't take focus back after a click elsewhere while locked", () => {
    const { rerender } = render(<><LockFieldset locked={false}><input aria-label="pw" /></LockFieldset><p>text</p></>)
    const pw = screen.getByLabelText('pw') as HTMLInputElement
    pw.focus()
    rerender(<><LockFieldset locked><input aria-label="pw" /></LockFieldset><p>text</p></>)
    fireEvent.pointerDown(screen.getByText('text'))
    Object.defineProperty(document, 'activeElement', { configurable: true, get: () => document.body })
    const focus = vi.spyOn(pw, 'focus')
    try {
      rerender(<><LockFieldset locked={false}><input aria-label="pw" /></LockFieldset><p>text</p></>)
    } finally {
      delete (document as unknown as Record<string, unknown>).activeElement
    }
    expect(focus).not.toHaveBeenCalled()
  })

  it("doesn't take focus back from where the user has moved it", () => {
    const { rerender } = render(<><LockFieldset locked={false}><input aria-label="pw" /></LockFieldset><input aria-label="other" /></>)
    const pw = screen.getByLabelText('pw') as HTMLInputElement
    pw.focus()
    rerender(<><LockFieldset locked><input aria-label="pw" /></LockFieldset><input aria-label="other" /></>)
    const other = screen.getByLabelText('other') as HTMLInputElement
    other.focus()
    rerender(<><LockFieldset locked={false}><input aria-label="pw" /></LockFieldset><input aria-label="other" /></>)
    expect(document.activeElement).toBe(other)
  })
})
