import React from 'react'

type Props = {
  // An error (danger colours, announced at once) or a confirmation.
  error?: boolean
  children: React.ReactNode
}

// The one-line result box under a form: "saved", "wrong password", ….
export function Notice({ error = false, children }: Props) {
  return (
    <div role={error ? 'alert' : 'status'} style={{
      padding: '8px 12px',
      borderRadius: 'var(--radius-md)',
      background: error ? 'var(--color-danger-bg)' : 'var(--color-success-bg)',
      border: `1px solid ${error ? 'var(--color-danger-border)' : 'var(--color-success-border)'}`,
      color: error ? 'var(--color-danger-fg)' : 'var(--color-success-fg)',
      fontSize: 'var(--font-size-sm)',
    }}>
      {children}
    </div>
  )
}
