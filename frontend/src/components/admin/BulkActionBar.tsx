import React from 'react'
import { useI18n } from '../../lib/i18n'
import { Button } from '../ui'
import PhaseStatus from './PhaseStatus'
import BulkSelectionBar from './BulkSelectionBar'

type Props = {
  selectedCount: number
  totalVisible: number
  allVisibleSelected: boolean
  canManage: boolean
  // Export is admin-only (the bundle carries the raw WSI + label images).
  canExport: boolean
  busy: boolean
  exportBusy: boolean
  // What the server reports of a running export (bytes written so far), shown
  // beside the busy button; null / absent when there is nothing to show.
  exportProgress?: string | null
  // The export job's phase: screen readers hear about a new phase, not about
  // every poll's byte count (see PhaseStatus).
  exportPhase?: string | null
  onSelectAll: () => void
  onClear: () => void
  onBulkPublish: (publish: boolean) => void
  onBulkDelete: () => void
  onOpenCategory: () => void
  onExportSelected: () => void
}

export default function BulkActionBar({
  selectedCount, totalVisible, allVisibleSelected, canManage, canExport, busy, exportBusy,
  exportProgress, exportPhase, onSelectAll, onClear, onBulkPublish, onBulkDelete, onOpenCategory, onExportSelected,
}: Props) {
  const { t } = useI18n()
  return (
    <BulkSelectionBar
      selectedCount={selectedCount}
      allVisibleSelected={allVisibleSelected}
      selectAllLabel={t('adminDash.bulkSelectAll', { count: totalVisible })}
      selectedLabel={t('adminDash.bulkSelected', { count: selectedCount })}
      busy={busy}
      onSelectAll={onSelectAll}
      onClear={onClear}
    >
      <Button size="sm" variant="secondary" disabled={busy} onClick={() => onBulkPublish(true)}>
        {t('adminDash.bulkPublish')}
      </Button>
      <Button size="sm" variant="secondary" disabled={busy} onClick={() => onBulkPublish(false)}>
        {t('adminDash.bulkUnpublish')}
      </Button>
      {canManage && (
        <Button size="sm" variant="secondary" disabled={busy} onClick={onOpenCategory}>
          {t('adminDash.bulkCategory')}
        </Button>
      )}
      {canExport && (
        <Button size="sm" variant="secondary" disabled={busy || exportBusy} onClick={onExportSelected}>
          {exportBusy ? t('adminDash.exportPreparing') : t('adminDash.exportSelected')}
        </Button>
      )}
      {canExport && exportBusy && exportProgress && (
        <PhaseStatus
          text={exportProgress}
          phase={exportPhase ?? ''}
          style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-subtle)' }}
        />
      )}
      {canManage && (
        <Button size="sm" variant="danger" disabled={busy} onClick={onBulkDelete}>
          {t('adminDash.bulkDelete')}
        </Button>
      )}
    </BulkSelectionBar>
  )
}
