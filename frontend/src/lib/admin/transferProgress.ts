// The line of text a running transfer job's progress stands for, or null when
// it has nothing to show yet (queued, done, an unknown phase). Shapes are the
// backend's (routers/admin_slide_transfer.py + slide_transfer.py; the job
// store merges each update into the previous progress):
//   export          phase "queued" / "writing_wsi" / "finalizing", bytes_written
//                   (no total: the archive is compressed as it is written)
//   import staging  phase "copying" (bytes_copied / total_bytes: the received
//                   bundle copied into staging), "extracting", "checking"
//   apply           phase "queued" / "importing" (done / total slides)
//   retry-assets    phase "copying_assets" (done / total attachments)
import type { TransferJob } from '../api'
import { formatBytes } from './validation'

type TFn = (k: string, p?: Record<string, string | number>) => string

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null
}

export function transferProgressText(t: TFn, progress: TransferJob['progress'] | null | undefined): string | null {
  if (!progress) return null
  const done = num(progress.done)
  const total = num(progress.total)
  switch (progress.phase) {
    case 'writing_wsi':
    case 'finalizing': {
      const written = num(progress.bytes_written)
      return written === null
        ? null
        : t(`adminTransfer.progress.${progress.phase}`, { written: formatBytes(written) })
    }
    case 'copying': {
      const copied = num(progress.bytes_copied)
      const size = num(progress.total_bytes)
      if (copied === null || !size) return null
      return t('adminTransfer.progress.copying', {
        copied: formatBytes(copied),
        total: formatBytes(size),
        pct: Math.min(100, Math.floor((copied / size) * 100)),
      })
    }
    case 'extracting':
    case 'checking':
      return t(`adminTransfer.progress.${progress.phase}`)
    case 'importing':
    case 'copying_assets':
      return done === null || !total
        ? null
        : t(`adminTransfer.progress.${progress.phase}`, { done, total })
    default:
      return null
  }
}
