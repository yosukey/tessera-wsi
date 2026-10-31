import React from 'react'
import { useI18n } from '../../lib/i18n'

type Props = {
  expanded: boolean
  // How many tours are currently hidden. Only read in the collapsed state —
  // the expanded label is a plain "show less" with no count.
  hiddenCount: number
  onToggle: () => void
}

/**
 * The "+N more" / "Show less" toggle under a slide's inline tour list.
 *
 * Shared by the tile card and the list row so both views expand and collapse
 * with the same affordance and the same label. It is deliberately a real
 * <button> with aria-expanded rather than a styled <span>: the tours it
 * reveals are the page's own content, and both call sites place it *outside*
 * the surrounding slide link so activating it never navigates away.
 */
export default function TourExpander({ expanded, hiddenCount, onToggle }: Props) {
  const { t } = useI18n()
  return (
    <button
      type="button"
      className="btn-reset"
      aria-expanded={expanded}
      onClick={onToggle}
      style={{
        fontSize: 'var(--font-size-xs)',
        color: 'var(--color-text-subtle)',
        textAlign: 'left',
        alignSelf: 'flex-start',
        padding: '2px 0',
        cursor: 'pointer',
      }}
    >
      {expanded ? t('index.toursCollapse') : t('index.toursMore', { count: hiddenCount })}
    </button>
  )
}
