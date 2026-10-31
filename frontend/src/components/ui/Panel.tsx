import React from 'react'
import styles from './Panel.module.css'

type Tone = 'default' | 'muted' | 'flat'
type Padding = 'none' | 'sm' | 'md' | 'lg'

type Props = React.HTMLAttributes<HTMLDivElement> & {
  tone?: Tone
  padding?: Padding
}

const toneClass: Record<Tone, string> = {
  default: '',
  muted: styles.toneMuted,
  flat: styles.toneFlat,
}
const padClass: Record<Padding, string> = {
  none: '',
  sm: styles.padSm,
  md: styles.padMd,
  lg: styles.padLg,
}

export function Panel({ tone = 'default', padding = 'md', className, children, ...rest }: Props) {
  const cls = [
    styles.root,
    toneClass[tone],
    padClass[padding],
    className ?? '',
  ].filter(Boolean).join(' ')
  return <div className={cls} {...rest}>{children}</div>
}
