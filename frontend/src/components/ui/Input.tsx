import React from 'react'
import styles from './Input.module.css'

type Size = 'sm' | 'md'
type Props = Omit<React.InputHTMLAttributes<HTMLInputElement>, 'size'> & {
  size?: Size
  leftAdornment?: React.ReactNode
  rightAdornment?: React.ReactNode
}

export function Input({
  size = 'md',
  leftAdornment,
  rightAdornment,
  className,
  ...rest
}: Props) {
  const cls = [
    styles.root,
    size === 'sm' ? styles.sizeSm : styles.sizeMd,
    leftAdornment ? styles.withLeft : '',
    rightAdornment ? styles.withRight : '',
    className ?? '',
  ].filter(Boolean).join(' ')

  if (!leftAdornment && !rightAdornment) {
    return <input className={cls} {...rest} />
  }
  return (
    <span className={styles.wrap}>
      {leftAdornment && <span className={styles.adornmentLeft}>{leftAdornment}</span>}
      <input className={cls} {...rest} />
      {rightAdornment && <span className={styles.adornmentRight}>{rightAdornment}</span>}
    </span>
  )
}
