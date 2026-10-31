// @vitest-environment jsdom
import React, { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  adminMfaSetup: vi.fn(),
  adminMfaConfirm: vi.fn(),
  adminMfaDisable: vi.fn(),
  adminMfaRegenerateRecoveryCodes: vi.fn(),
}))
vi.mock('../../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/api')>()),
  ...api,
}))
vi.mock('qrcode', () => ({ default: { toDataURL: vi.fn().mockResolvedValue('data:image/png;base64,AAAA') } }))

import type { MfaStatus } from '../../lib/api'
import { I18nProvider } from '../../lib/i18n'
import { STORAGE_KEYS } from '../../lib/storageKeys'
import AccountMfaSection from './AccountMfaSection'

const OFF: MfaStatus = { enabled: false, enabled_at: null, recovery_codes_remaining: 0, available: true }
const ON: MfaStatus = { enabled: true, enabled_at: '2026-10-26T00:00:00+00:00', recovery_codes_remaining: 10, available: true }

function Harness({ initial }: { initial: MfaStatus }) {
  const [mfa, setMfa] = useState(initial)
  return (
    <I18nProvider>
      <AccountMfaSection username="alice" mfa={mfa} onChange={setMfa} />
    </I18nProvider>
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  // Undo vi.spyOn(window, 'confirm') so it cannot leak into later tests.
  vi.restoreAllMocks()
  sessionStorage.clear()
})

describe('AccountMfaSection', () => {
  it('locks the form while its request is out', async () => {
    // A failure clears the code, so one typed meanwhile would be wiped.
    api.adminMfaDisable.mockReturnValue(new Promise(() => {}))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<Harness initial={ON} />)
    const password = screen.getByLabelText('Current password') as HTMLInputElement
    fireEvent.change(password, { target: { value: 'pw' } })
    const code = screen.getByLabelText(/recovery code/i) as HTMLInputElement
    fireEvent.change(code, { target: { value: '123456' } })
    expect(code.closest('fieldset')!.disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    await waitFor(() => expect(code.closest('fieldset')!.disabled).toBe(true))
    expect(code.closest('fieldset')!.contains(password)).toBe(true)
  })

  it('turns two-factor on: password, QR code, first code, recovery codes', async () => {
    api.adminMfaSetup.mockResolvedValue({
      secret: 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP',
      otpauth_uri: 'otpauth://totp/x',
      issuer: 'Tessera',
      account: 'alice',
      setup_token: 'setup',
      expires_in: 600,
    })
    api.adminMfaConfirm.mockResolvedValue({ recovery_codes: ['AAAAA-BBBBB', 'CCCCC-DDDDD'], mfa: ON })
    render(<Harness initial={OFF} />)
    expect(screen.getByText('Off')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Current password (for verification)'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    expect(await screen.findByTestId('mfa-secret')).toBeTruthy()
    expect(api.adminMfaSetup).toHaveBeenCalledWith('pw')
    expect(await screen.findByAltText(/QR code/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('2. Enter the 6-digit code the app shows.'), { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm and turn on' }))
    expect((await screen.findByTestId('mfa-recovery-codes')).textContent).toContain('AAAAA-BBBBB')
    expect(api.adminMfaConfirm).toHaveBeenCalledWith('setup', '123456')
    expect(screen.getByText('On')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'I have saved these codes' }))
    expect(screen.queryByTestId('mfa-recovery-codes')).toBeNull()
  })

  it('says so when the server cannot enrol (no WSI_MFA_KEY)', () => {
    render(<Harness initial={{ ...OFF, available: false }} />)
    expect(screen.getByText(/WSI_MFA_KEY is not configured/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Turn on' })).toBeNull()
  })

  it('turns two-factor off with the password and a code', async () => {
    api.adminMfaDisable.mockResolvedValue({ mfa: OFF })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<Harness initial={ON} />)
    expect(screen.getByText('10 recovery code(s) left')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'pw' } })
    fireEvent.change(screen.getByLabelText('Authentication code or recovery code'), { target: { value: '654321' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    await screen.findByText('Two-factor authentication is off.')
    expect(api.adminMfaDisable).toHaveBeenCalledWith('pw', '654321')
    expect(screen.getByText('Off')).toBeTruthy()
  })
})

describe('AccountMfaSection with no recovery code left', () => {
  it('does not offer the shortcut to a session that signed in with an authenticator code', () => {
    render(<Harness initial={{ ...ON, recovery_codes_remaining: 0 }} />)
    expect(screen.queryByText(/leave the code empty/)).toBeNull()
  })

  it('lets the code be left empty, for the session that spent the last recovery code', async () => {
    const payload = btoa(JSON.stringify({ sub: '1', amr: 'recovery' })).replace(/=+$/, '')
    sessionStorage.setItem(STORAGE_KEYS.adminToken, `h.${payload}.s`)
    api.adminMfaRegenerateRecoveryCodes.mockResolvedValue({ recovery_codes: ['AAAAA-BBBBB'], mfa: ON })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<Harness initial={{ ...ON, recovery_codes_remaining: 0 }} />)
    expect(screen.getByText(/leave the code empty/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Current password'), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: 'New recovery codes' }))
    expect((await screen.findByTestId('mfa-recovery-codes')).textContent).toContain('AAAAA-BBBBB')
    expect(api.adminMfaRegenerateRecoveryCodes).toHaveBeenCalledWith('pw', '')
  })
})
