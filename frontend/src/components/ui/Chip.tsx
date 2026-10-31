import React from 'react'
import styles from './Chip.module.css'

type Props = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  active?: boolean
  count?: number
}

export function Chip({ active, count, className, children, ...rest }: Props) {
  const cls = [
    'btn-reset',
    styles.root,
    active ? styles.active : '',
    className ?? '',
  ].filter(Boolean).join(' ')
  return (
    <button className={cls} type="button" {...rest}>
      {children}
      {count !== undefined && <span className={styles.count}>{count}</span>}
    </button>
  )
}
