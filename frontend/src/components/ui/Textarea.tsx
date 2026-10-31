import React from 'react'
import styles from './Textarea.module.css'

type Props = React.TextareaHTMLAttributes<HTMLTextAreaElement>

export function Textarea({ className, ...rest }: Props) {
  const cls = [styles.root, className ?? ''].filter(Boolean).join(' ')
  return <textarea className={cls} {...rest} />
}
