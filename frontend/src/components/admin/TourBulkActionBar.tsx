import React from 'react'
import { useI18n } from '../../lib/i18n'
import { Visibility, VISIBILITY_ORDER } from '../../lib/visibility'
import { Button } from '../ui'
import BulkSelectionBar from './BulkSelectionBar'

// The tours' counterpart of BulkActionBar: tours are selected separately from
// the slides (a slide action such as unpublishing also moves its tours, so one
// mixed selection would leave it unclear what an action applies to), and get
// a bar of their own beneath the slides' one. Every action here is open to
// any staff role, as the single-tour endpoints are.
type Props = {
  selectedCount: number
  totalVisible: number
  allVisibleSelected: boolean
  busy: boolean
  onSelectAll: () => void
  onClear: () => void
  onVisibility: (target: Visibility) => void
  onOpenCategory: () => void
  onDelete: () => void
}

export default function TourBulkActionBar({
  selectedCount, totalVisible, allVisibleSelected, busy,
  onSelectAll, onClear, onVisibility, onOpenCategory, onDelete,
}: Props) {
  const { t } = useI18n()
  const visLabels: Record<Visibility, string> = {
    draft: t('adminDash.visDraft'),
    unlisted: t('adminDash.visLinkOnly'),
    public: t('adminDash.visPublic'),
  }
  const selectedLabel = t('adminDash.bulkToursSelected', { count: selectedCount })
  return (
    <BulkSelectionBar
      selectedCount={selectedCount}
      allVisibleSelected={allVisibleSelected}
      selectAllLabel={t('adminDash.bulkSelectAllTours', { count: totalVisible })}
      selectedLabel={selectedLabel}
      busy={busy}
      onSelectAll={onSelectAll}
      onClear={onClear}
      ariaLabel={selectedLabel}
    >
      <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-muted)' }}>{t('adminDash.visLabel')}:</span>
      {VISIBILITY_ORDER.map((v) => (
        <Button key={v} size="sm" variant="secondary" disabled={busy} onClick={() => onVisibility(v)}>
          {visLabels[v]}
        </Button>
      ))}
      <Button size="sm" variant="secondary" disabled={busy} onClick={onOpenCategory}>
        {t('adminDash.bulkTourCategory')}
      </Button>
      <Button size="sm" variant="danger" disabled={busy} onClick={onDelete}>
        {t('adminDash.bulkDelete')}
      </Button>
    </BulkSelectionBar>
  )
}
