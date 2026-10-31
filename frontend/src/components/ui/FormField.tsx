import React from 'react'
import styles from './FormField.module.css'

type Props = {
  label?: React.ReactNode
  description?: React.ReactNode
  error?: React.ReactNode
  required?: boolean
  children: React.ReactNode
  className?: string
  style?: React.CSSProperties
  as?: 'label' | 'div'
}

export function FormField({
  label, description, error, required, children, className, style, as = 'label',
}: Props) {
  const Tag: any = as
  const cls = [styles.root, className ?? ''].filter(Boolean).join(' ')
  return (
    <Tag className={cls} style={style}>
      {label && (
        <span className={styles.label}>
          {label}
          {required && <span className={styles.required}>*</span>}
        </span>
      )}
      {children}
      {description && !error && <span className={styles.description}>{description}</span>}
      {error && <span className={styles.error}>{error}</span>}
    </Tag>
  )
}
