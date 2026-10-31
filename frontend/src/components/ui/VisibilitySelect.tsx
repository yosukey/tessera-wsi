import React from 'react'
import styles from './VisibilitySelect.module.css'
import { Visibility, VISIBILITY_ORDER } from '../../lib/visibility'

export type VisibilitySelectSize = 'sm' | 'md'

type Props = {
  value: Visibility
  onChange: (next: Visibility) => void
  labels: Record<Visibility, string>
  size?: VisibilitySelectSize
  // Whole control busy (a change is in flight): every segment is inert.
  disabled?: boolean
  // Disable the two *published* segments (link-only / public) — e.g. while the
  // slide is missing the catalog id / title required to go public. Draft stays
  // selectable so an already-published item can always be taken back to draft.
  publishDisabled?: boolean
  publishDisabledTitle?: string
  // Tooltip on the link-only segment explaining what it does.
  linkOnlyTitle?: string
  ariaLabel?: string
}

const sizeClass: Record<VisibilitySelectSize, string> = {
  sm: styles.sizeSm,
  md: styles.sizeMd,
}

const levelClass: Record<Visibility, string> = {
  draft: styles.draft,
  unlisted: styles.unlisted,
  public: styles.public,
}

export function VisibilitySelect({
  value,
  onChange,
  labels,
  size = 'md',
  disabled,
  publishDisabled,
  publishDisabledTitle,
  linkOnlyTitle,
  ariaLabel,
}: Props) {
  return (
    <div role="group" aria-label={ariaLabel} className={[styles.root, sizeClass[size]].join(' ')}>
      {VISIBILITY_ORDER.map((level) => {
        const selected = value === level
        // 'draft' is always reachable; the published levels honour publishDisabled.
        const publishBlocked = !!publishDisabled && level !== 'draft'
        const segDisabled = !!disabled || publishBlocked
        // When a published segment is blocked, its tooltip must explain WHY it
        // can't be clicked — that reason wins over the link-only hint, which
        // would otherwise be all a hovering admin sees on the disabled
        // 'unlisted' segment.
        const title = publishBlocked
          ? publishDisabledTitle
          : (level === 'unlisted' ? linkOnlyTitle : undefined)
        const cls = [
          'btn-reset',
          styles.seg,
          levelClass[level],
          selected ? styles.selected : '',
        ].filter(Boolean).join(' ')
        return (
          <button
            key={level}
            type="button"
            className={cls}
            aria-pressed={selected}
            disabled={segDisabled}
            title={title}
            onClick={() => { if (!selected && !segDisabled) onChange(level) }}
          >
            {labels[level]}
          </button>
        )
      })}
    </div>
  )
}
