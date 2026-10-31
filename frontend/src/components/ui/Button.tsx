import React from 'react'
import styles from './Button.module.css'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md' | 'lg'

type Props = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  fullWidth?: boolean
}

const variantClass: Record<ButtonVariant, string> = {
  primary: styles.primary,
  secondary: styles.secondary,
  ghost: styles.ghost,
  danger: styles.danger,
}
const sizeClass: Record<ButtonSize, string> = {
  sm: styles.sizeSm,
  md: styles.sizeMd,
  lg: styles.sizeLg,
}

export const Button = React.forwardRef<HTMLButtonElement, Props>(function Button({
  variant = 'secondary',
  size = 'md',
  loading, fullWidth,
  className, children, disabled, ...rest
}, ref) {
  const cls = [
    'btn-reset',
    styles.root,
    variantClass[variant],
    sizeClass[size],
    fullWidth ? styles.fullWidth : '',
    className ?? '',
  ].filter(Boolean).join(' ')
  return (
    <button ref={ref} className={cls} disabled={disabled || loading} {...rest}>
      {loading && <span className={styles.spinner} aria-hidden />}
      {children}
    </button>
  )
})
