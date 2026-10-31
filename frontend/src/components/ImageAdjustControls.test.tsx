// @vitest-environment jsdom
import React, { useState } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '../lib/i18n'
import { DEFAULT_IMAGE_ADJUST, type ImageAdjust } from '../lib/imageAdjust'
import ImageAdjustControls from './ImageAdjustControls'

afterEach(cleanup)

// The viewer's side: it holds the value, and a save moves the baseline.
function Harness({ onSavePreset }: { onSavePreset: (v: ImageAdjust) => Promise<void> }) {
  const [value, setValue] = useState<ImageAdjust>(DEFAULT_IMAGE_ADJUST)
  const [baseline, setBaseline] = useState<ImageAdjust>(DEFAULT_IMAGE_ADJUST)
  return (
    <I18nProvider>
      <ImageAdjustControls
        value={value}
        onChange={setValue}
        baseline={baseline}
        canSavePreset
        onSavePreset={async (v) => { await onSavePreset(v); setBaseline(v) }}
      />
    </I18nProvider>
  )
}

describe('ImageAdjustControls preset save', () => {
  it('takes one save at a time, and only says "saved" for what is still shown', async () => {
    let finish: () => void = () => {}
    const onSavePreset = vi.fn(() => new Promise<void>((r) => { finish = r }))
    render(<Harness onSavePreset={onSavePreset} />)
    fireEvent.click(screen.getByRole('button', { name: 'Image adjustments' }))
    const [brightness] = screen.getAllByRole('slider') as HTMLInputElement[]
    fireEvent.change(brightness, { target: { value: '1.2' } })
    const save = screen.getByRole('button', { name: 'Save as preset' }) as HTMLButtonElement
    fireEvent.click(save)
    expect(onSavePreset).toHaveBeenCalledTimes(1)
    // Moving a slider while it is out keeps the save running, not re-armed.
    fireEvent.change(brightness, { target: { value: '1.4' } })
    const saving = screen.getByRole('button', { name: 'Saving…' }) as HTMLButtonElement
    expect(saving.disabled).toBe(true)
    fireEvent.click(saving)
    expect(onSavePreset).toHaveBeenCalledTimes(1)
    await act(async () => finish())
    // What is shown (1.4) is not what was saved (1.2): no "saved", and it can
    // be saved again.
    expect(screen.queryByText('Preset saved')).toBeNull()
    expect((screen.getByRole('button', { name: 'Save as preset' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('says "saved" when nothing moved meanwhile', async () => {
    const onSavePreset = vi.fn(async () => {})
    render(<Harness onSavePreset={onSavePreset} />)
    fireEvent.click(screen.getByRole('button', { name: 'Image adjustments' }))
    fireEvent.change(screen.getAllByRole('slider')[0], { target: { value: '1.2' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save as preset' })) })
    expect(screen.getByText('Preset saved')).toBeTruthy()
  })
})
