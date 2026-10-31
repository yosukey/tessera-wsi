import React from 'react'
import styles from './IconButton.module.css'

type Size = 'sm' | 'md'
type Props = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  size?: Size
  active?: boolean
}

const sizeClass: Record<Size, string> = {
  sm: styles.sizeSm,
  md: styles.sizeMd,
}

export function IconButton({ size = 'md', active, className, children, ...rest }: Props) {
  const cls = ['btn-reset', styles.root, sizeClass[size], className ?? ''].filter(Boolean).join(' ')
  return (
    <button className={cls} data-active={active ? 'true' : 'false'} {...rest}>
      {children}
    </button>
  )
}

type GroupProps = React.HTMLAttributes<HTMLDivElement>
export function IconButtonGroup({ className, children, ...rest }: GroupProps) {
  const cls = [styles.group, className ?? ''].filter(Boolean).join(' ')
  return <div className={cls} role="group" {...rest}>{children}</div>
}
