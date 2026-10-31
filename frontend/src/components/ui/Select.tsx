import React from 'react'
import styles from './Select.module.css'

type Size = 'sm' | 'md'
type Props = Omit<React.SelectHTMLAttributes<HTMLSelectElement>, 'size'> & {
  size?: Size
  fullWidth?: boolean
}

export function Select({ size = 'md', fullWidth, className, children, ...rest }: Props) {
  const cls = [
    styles.root,
    size === 'sm' ? styles.sizeSm : styles.sizeMd,
    fullWidth ? styles.fullWidth : '',
    className ?? '',
  ].filter(Boolean).join(' ')
  return <select className={cls} {...rest}>{children}</select>
}
