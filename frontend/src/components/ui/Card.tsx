import React from 'react'
import styles from './Card.module.css'

type Padding = 'none' | 'sm' | 'md' | 'lg'
type Props = React.HTMLAttributes<HTMLDivElement> & {
  padding?: Padding
  interactive?: boolean
  row?: boolean
}
const padClass: Record<Padding, string> = {
  none: styles.padNone,
  sm: styles.padSm,
  md: styles.padMd,
  lg: styles.padLg,
}

export function Card({ padding = 'none', interactive, row, className, children, ...rest }: Props) {
  const cls = [
    styles.root,
    padClass[padding],
    interactive ? styles.interactive : '',
    row ? styles.row : '',
    className ?? '',
  ].filter(Boolean).join(' ')
  return <div className={cls} {...rest}>{children}</div>
}

Card.Media = function CardMedia({ className, children, ...rest }: React.HTMLAttributes<HTMLDivElement>) {
  const cls = [styles.media, className ?? ''].filter(Boolean).join(' ')
  return <div className={cls} {...rest}>{children}</div>
}
