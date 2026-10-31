import React, { useState } from 'react'
import { apiErrorMessage } from '../../lib/admin/apiError'
import {
  adminDeleteTourAsset,
  adminListTourAssets,
  adminUploadTourAsset,
  isFileTooLargeError,
} from '../../lib/api'
import { formatByteLimit } from '../../lib/admin/validation'
import { useI18n } from '../../lib/i18n'
import { clientSafeFilename } from '../../lib/safeFilename'
import { useAdminBlobUrl } from '../../hooks/useAdminBlobUrl'
import { useInFlight } from '../../hooks/useInFlight'

type Asset = { name: string; url: string }

// Fetched with the admin bearer: under the ldap / google_oauth gate the asset
// URL needs app access, which a plain <img src> only has with an app cookie.
function AssetThumb({ url }: { url: string }) {
  const preview = useAdminBlobUrl(url)
  return <img src={preview || undefined} style={{ width: 52, height: 52, objectFit: 'cover', borderRadius: 8 }} />
}

interface Props {
  tourId: number
  // The tour's public_id token — asset URLs are keyed by it (the help text
  // below shows the ready-to-paste Markdown pattern). Upload/list/delete still
  // go through the numeric-id admin endpoints.
  tourPublicId: string
  assets: Asset[]
  setAssets: (a: Asset[]) => void
  // The server's per-file cap (TOUR_ASSET_MAX_BYTES) from the asset listing;
  // null while unknown, when only the server's own 413 refuses a file.
  maxBytes: number | null
  // Returns true if the markdown was actually appended to the step
  // description. When no step is in edit mode there is no editor to
  // insert into, and the parent returns false so the panel can surface
  // a hint instead of pretending the insert succeeded.
  onInsertMarkdown: (md: string) => boolean
  editorOpen: boolean
}

export default function TourAssetsPanel({ tourId, tourPublicId, assets, setAssets, maxBytes, onInsertMarkdown, editorOpen }: Props) {
  const { t } = useI18n()
  const [assetMsg, setAssetMsg] = useState('')
  const [assetDuplicatePolicy, setAssetDuplicatePolicy] = useState<'rename' | 'overwrite'>('rename')
  // One upload / delete at a time: each answer replaces the whole list with
  // its own fetch, so two out at once could land out of order (and a second
  // pick's collision check would read a list that is about to change).
  const assetRequest = useInFlight()
  const assetBusy = assetRequest.busy
  // The tour this panel is showing now. An upload / delete started in tour A
  // can finish after the admin moved to tour B; checking this before touching
  // state keeps A's asset list and status message out of B's editor.
  // Updated in a layout effect so it has switched before any already-pending
  // promise continuation can run against the new tour.
  const currentTourIdRef = React.useRef(tourId)
  React.useLayoutEffect(() => {
    currentTourIdRef.current = tourId
  }, [tourId])

  // The parent route reuses this component instance across tourId changes
  // (react-router only updates useParams); without this reset, the status
  // banner from tour A would still be shown after navigating into tour B.
  React.useEffect(() => {
    setAssetMsg('')
  }, [tourId])

  async function onUploadAsset(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    setAssetMsg('')
    // Overwrite policy is destructive: silently replacing a same-name asset
    // breaks every markdown ref that pointed at the prior file. Detect an
    // actual collision against the cached asset list before kicking off the
    // upload so the admin gets a per-file confirm only when it matters
    // (rather than on every upload after picking "overwrite").
    //
    // Compare on the server's canonical filename, not the raw File.name:
    // the server normalises e.g. "f o o.png" -> "f_o_o.png" before writing,
    // so a raw-name comparison would miss a collision where the safe-form
    // matches an existing asset and the upload would overwrite without
    // confirmation.
    const safeName = clientSafeFilename(file.name)
    if (assetDuplicatePolicy === 'overwrite' && assets.some((a) => a.name === safeName)) {
      if (!confirm(t('adminTour.overwriteAssetConfirm', { name: safeName }))) {
        e.target.value = ''
        return
      }
    }
    await assetRequest.run(async () => {
      const startedFor = tourId
      try {
        const r = await adminUploadTourAsset(tourId, file, assetDuplicatePolicy, maxBytes)
        const a = await adminListTourAssets(tourId)
        if (currentTourIdRef.current !== startedFor) return
        setAssetMsg(t('adminTour.assetUploaded', { name: r.name }))
        setAssets(a.items || [])
      } catch (err: any) {
        if (currentTourIdRef.current !== startedFor) return
        if (isFileTooLargeError(err)) {
          setAssetMsg(maxBytes != null
            ? t('common.fileTooLarge', { limit: formatByteLimit(maxBytes) })
            : t('common.fileTooLargeServer'))
        } else {
          setAssetMsg(apiErrorMessage(err, t) || t('adminTour.assetUploadFailed'))
        }
      } finally {
        e.target.value = ''
      }
    })
  }

  async function onDeleteAsset(name: string) {
    if (!confirm(t('adminTour.deleteAssetConfirm', { name }))) return
    await assetRequest.run(async () => {
      setAssetMsg('')
      const startedFor = tourId
      try {
        await adminDeleteTourAsset(tourId, name)
        const a = await adminListTourAssets(tourId)
        if (currentTourIdRef.current !== startedFor) return
        setAssets(a.items || [])
      } catch (err: any) {
        if (currentTourIdRef.current !== startedFor) return
        // Mirror the upload path: without this, a server 4xx/5xx (or a
        // network drop) leaves the asset visible in the list with no
        // indication that the delete failed, so the admin can't tell
        // whether to retry.
        setAssetMsg(apiErrorMessage(err, t) || t('adminTour.assetDeleteFailed'))
      }
    })
  }

  function insert(md: string) {
    const ok = onInsertMarkdown(md)
    setAssetMsg(t(ok ? 'adminTour.assetInserted' : 'adminTour.selectStepToInsertAsset'))
  }

  return (
    <>
      <h3 style={{ marginTop: 0 }}>{t('adminTour.assetsTitle')}</h3>
      <div style={{ fontSize: 12, opacity: 0.75, marginBottom: 8 }}>
        {t('adminTour.assetsHelp')} <code>{`![caption](/assets/tours/${tourPublicId}/file.png)`}</code> {t('adminTour.assetsHelpSuffix')}
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
        <input type="file" accept="image/*" onChange={onUploadAsset} disabled={assetBusy} />
        <label style={{ fontSize: 12 }}>{t('adminTour.duplicateFileLabel')}</label>
        <select value={assetDuplicatePolicy} disabled={assetBusy} onChange={(e) => setAssetDuplicatePolicy(e.target.value as 'rename' | 'overwrite')}>
          <option value="rename">{t('adminTour.duplicateRename')}</option>
          <option value="overwrite">{t('adminTour.duplicateOverwrite')}</option>
        </select>
      </div>
      {!editorOpen && (
        <div style={{ fontSize: 12, marginTop: 6, opacity: 0.7 }}>
          {t('adminTour.selectStepToInsertAsset')}
        </div>
      )}
      {assetMsg && <div style={{ fontSize: 12, marginTop: 6, whiteSpace: 'pre-wrap', opacity: 0.85 }}>{assetMsg}</div>}
      {assets.length > 0 && (
        <div style={{ marginTop: 10, display: 'grid', gap: 8 }}>
          {assets.map((a) => (
            <div key={a.name} style={{ border: '1px solid var(--color-border)', borderRadius: 10, padding: 8, display: 'flex', gap: 8, alignItems: 'center' }}>
              <AssetThumb url={a.url} />
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12, fontWeight: 700 }}>{a.name}</div>
                <div style={{ fontSize: 12, opacity: 0.75 }}><code>{a.url}</code></div>
                <div style={{ fontSize: 12, opacity: 0.75 }}>{t('adminTour.markdownLabel')}: <code>{`![${a.name}](${a.url})`}</code></div>
              </div>
              <div style={{ display: 'grid', gap: 6 }}>
                <button
                  onClick={() => insert(`![${a.name}](${a.url})`)}
                  disabled={!editorOpen}
                  title={editorOpen ? undefined : t('adminTour.selectStepToInsertAsset')}
                >
                  {t('adminTour.insert')}
                </button>
                <button onClick={() => onDeleteAsset(a.name)} disabled={assetBusy}>{t('adminTour.delete')}</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
