import React from 'react'
import styles from './Badge.module.css'

export type BadgeTone = 'neutral' | 'brand' | 'success' | 'warning' | 'danger' | 'info'

type Props = React.HTMLAttributes<HTMLSpanElement> & {
  tone?: BadgeTone
  pill?: boolean
}

const toneClass: Record<BadgeTone, string> = {
  neutral: styles.toneNeutral,
  brand: styles.toneBrand,
  success: styles.toneSuccess,
  warning: styles.toneWarning,
  danger: styles.toneDanger,
  info: styles.toneInfo,
}

export function Badge({ tone = 'neutral', pill, className, children, ...rest }: Props) {
  const cls = [
    styles.root,
    toneClass[tone],
    pill ? styles.pill : '',
    className ?? '',
  ].filter(Boolean).join(' ')
  return <span className={cls} {...rest}>{children}</span>
}
