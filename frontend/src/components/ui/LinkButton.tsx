import React from 'react'
import { Link, type LinkProps } from 'react-router'
import styles from './Button.module.css'
import type { ButtonVariant, ButtonSize } from './Button'

type Props = LinkProps & {
  variant?: ButtonVariant
  size?: ButtonSize
  rightIcon?: React.ReactNode
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

export function LinkButton({
  variant = 'secondary',
  size = 'md',
  rightIcon, fullWidth,
  className, children, ...rest
}: Props) {
  const cls = [
    'btn-reset',
    styles.root,
    variantClass[variant],
    sizeClass[size],
    fullWidth ? styles.fullWidth : '',
    className ?? '',
  ].filter(Boolean).join(' ')
  return (
    <Link className={cls} {...rest}>
      {children}
      {rightIcon}
    </Link>
  )
}
