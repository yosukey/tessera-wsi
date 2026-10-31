import React, { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useDialogFocus } from '../../hooks/useDialogFocus'
import { useInFlight } from '../../hooks/useInFlight'
import {
  MetadataOptions,
  Slide,
  adminDownloadSlide,
  adminFetchImageBlobUrl,
  adminGetSlide,
  adminGetStorage,
  adminReplaceSlideWsi,
  adminUpdateSlide,
  isAbortError,
  isAdminFromToken,
  isFileTooLargeError,
  isGatewayError,
  isNetworkError,
  isRateLimitError,
} from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { logger } from '../../lib/logger'
import {
  validateMetadata,
  validateMppInput,
  formatMpp,
  formatBytes,
  displayFilename,
  BASE_KEYS,
  ACCEPTED_WSI_EXTENSIONS,
  acceptedWsiExtensions,
  exceedsUploadLimit,
  formatByteLimit,
} from '../../lib/admin/validation'
import type { AttrMap, MetadataBaseline } from '../../lib/admin/validation'
import { apiErrorMessage, failureMessage } from '../../lib/admin/apiError'
import { formatInEmbeddedOffset } from '../../lib/admin/datetime'
import OverlayOverrideRow from './OverlayOverrideRow'
import OptionMultiSelect from './OptionMultiSelect'
import { LockFieldset } from '../ui'
import AdjustSliderRow from '../AdjustSliderRow'
import SlideCredit from '../SlideCredit'
import { CREDIT_TEXT_MAX_LEN, SLIDE_LICENSES, SOURCE_URL_MAX_LEN, licenseLabel, safeSourceUrl } from '../../lib/slideCredit'
import {
  IMAGE_ADJUST_RANGES,
  ImageAdjust,
  imageAdjustFromSlide,
  isImageAdjustDefault,
} from '../../lib/imageAdjust'

// Render the ISO 8601 acquired_at string from the backend in the user's
// locale. Falls back to an em-dash for empty / unparseable values rather
// than echoing the raw string, so vendor formats that store a malformed
// date don't surface as cryptic noise in the metadata panel. The backend
// emits either a date-only ("YYYY-MM-DD") or a full local timestamp
// ("YYYY-MM-DDTHH:MM:SS") with no zone — the latter is treated as a wall
// clock reading and both portions are formatted.
function formatAcquiredAt(raw: string | undefined): string {
  if (!raw) return '—'
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(raw)
  const d = new Date(dateOnly ? raw + 'T00:00:00' : raw)
  if (isNaN(d.getTime())) return '—'
  if (dateOnly) return d.toLocaleDateString()
  return d.toLocaleString()
}

type Props = {
  slide: Slide
  metaOptions: MetadataOptions
  customMetaKeys: string[]
  // null while the global defaults are unknown (still loading, or the fetch
  // failed): the overlay rows are then read-only and the per-slide values are
  // saved back unchanged, rather than re-derived against a guessed default.
  globalOverlays: {
    showZoomIndicator: boolean
    showScaleBar: boolean
    showMetadataPanel: boolean
  } | null
  // Credit texts already used on other slides, offered as completions so
  // slides from the same source are credited identically.
  creditSuggestions?: string[]
  onClose: () => void
  onSaved: () => Promise<void>
}

type Feedback = { text: string; tone: 'success' | 'error' } | null

// The MPP form fields as seeded from a slide (blank = no value), and the
// (x, y) pair an unedited form would submit — onSubmit mirrors Y from X
// unless the axes are split.
function mppFormFromSlide(s: Slide) {
  const x = s.mpp_x ? String(s.mpp_x) : ''
  const y = s.mpp_y ? String(s.mpp_y) : ''
  const split = !!(s.mpp_x && s.mpp_y) && s.mpp_x !== s.mpp_y
  return { x, y, split, submitted: { x: x.trim(), y: (split ? y : x).trim() } }
}

type CreditForm = { credit_text: string; license_id: string; source_url: string }

function creditFormFromSlide(s: Slide): CreditForm {
  return {
    credit_text: s.credit_text || '',
    license_id: s.license_id || '',
    source_url: s.source_url || '',
  }
}

function trimCredit(c: CreditForm): CreditForm {
  return { credit_text: c.credit_text.trim(), license_id: c.license_id, source_url: c.source_url.trim() }
}

function sameCredit(a: CreditForm, b: CreditForm): boolean {
  return a.credit_text === b.credit_text && a.license_id === b.license_id && a.source_url === b.source_url
}

function metaBaselineFromSlide(s: Slide): MetadataBaseline {
  return {
    fields: { catalog_id: s.catalog_id || '', title: s.title || '' },
    attrs: s.attributes || {},
  }
}

export default function SlideEditModal({ slide, metaOptions, customMetaKeys, globalOverlays, creditSuggestions = [], onClose, onSaved }: Props) {
  const { t } = useI18n()
  // Track text + tone so the success / danger styling doesn't depend on
  // locale-specific string equality with `t('adminDash.saved')` etc.,
  // which breaks the moment the translation includes a suffix.
  const [editMsg, setEditMsg] = useState<Feedback>(null)
  const [savingEdit, setSavingEdit] = useState(false)
  // The fresh preset fetch (see below) has settled.
  const [presetChecked, setPresetChecked] = useState(false)
  const [wsiUploadProgress, setWsiUploadProgress] = useState<number | null>(null)
  // A WSI replace from the file pick on, its pre-checks included: the MPP
  // fields and a second pick wait for it (see uploadInFlight).
  const replaceRequest = useInFlight()
  // Once the bytes have left the browser (progress hits 100%) the server still
  // validates and thumbnails the archive, which for a multi-GB WSI can take
  // many seconds. Flip the button to a "processing" label so the operator
  // doesn't stare at a stuck "uploading 100%" (mirrors AdminBackupSection's
  // uploadStaging).
  const [wsiStaging, setWsiStaging] = useState(false)
  const [wsiMsg, setWsiMsg] = useState<Feedback>(null)
  const [wsiDownloading, setWsiDownloading] = useState(false)
  const canDownloadWsi = isAdminFromToken()
  const wsiFileRef = useRef<HTMLInputElement | null>(null)
  // Lazy-initialise from the slide prop so the very first render already
  // reflects the persisted DB values. Without this the form briefly renders
  // with default empty/null state before the form-init effect catches up,
  // which can read as "the checkboxes reverted to pre-save state" especially
  // on slow renders or when the user scans the modal as it appears.
  const [editCatalogId, setEditCatalogId] = useState(() => slide.catalog_id || '')
  const [editTitle, setEditTitle] = useState(() => slide.title || '')
  // Whether the catalog ID / title were non-empty when last seeded (or last
  // saved). The clear_* flags are sent only when the operator blanked a value
  // that was actually stored: a slide whose title is already blank (possible
  // via bundle import) must not send clear_title on every unrelated save, since
  // the server refuses a clear on a published slide.
  const storedTextRef = useRef({
    catalogId: !!(slide.catalog_id || '').trim(),
    title: !!(slide.title || '').trim(),
  })
  // The catalog ID / title / attributes as last seeded or saved. Only what
  // differs from them is validated on save (see validateMetadata).
  const metaBaselineRef = useRef<MetadataBaseline>(metaBaselineFromSlide(slide))
  const [editSelections, setEditSelections] = useState<Record<string, string[]>>(() => {
    const attrsMap: Record<string, string[]> = slide.attributes || {}
    const selections: Record<string, string[]> = {}
    Array.from(BASE_KEYS).forEach(k => { selections[k] = attrsMap[k] ? [...attrsMap[k]] : [] })
    Object.entries(attrsMap).forEach(([k, v]) => {
      if (BASE_KEYS.has(k)) return
      selections[k] = Array.isArray(v) ? [...v] : []
    })
    return selections
  })
  const [editMppX, setEditMppX] = useState(() => slide.mpp_x ? String(slide.mpp_x) : '')
  const [editMppY, setEditMppY] = useState(() => slide.mpp_y ? String(slide.mpp_y) : '')
  const [editMppSplit, setEditMppSplit] = useState(
    () => !!(slide.mpp_x && slide.mpp_y) && slide.mpp_x !== slide.mpp_y
  )
  // What the MPP fields were last seeded with (or last saved as). onSubmit
  // only sends mpp_x / mpp_y when the operator changed them: the form is
  // seeded once per slide id, so a value the server changed since — a WSI
  // replacement resets MPP to the new file's — would otherwise be written
  // back as a manual override on the next unrelated save.
  const mppBaselineRef = useRef(mppFormFromSlide(slide).submitted)
  // Bumped by every seedMpp, so a save that was in flight across a WSI
  // replace doesn't put its pre-replace MPP back as the baseline (the next
  // save would then write the new file's MPP as a manual override).
  const mppSeedGenRef = useRef(0)
  function seedMpp(s: Slide) {
    const f = mppFormFromSlide(s)
    setEditMppX(f.x)
    setEditMppY(f.y)
    setEditMppSplit(f.split)
    mppBaselineRef.current = f.submitted
    mppSeedGenRef.current += 1
  }
  const [editShowZoomIndicator, setEditShowZoomIndicator] = useState<boolean | null>(
    () => slide.show_zoom_indicator ?? null
  )
  const [editShowScaleBar, setEditShowScaleBar] = useState<boolean | null>(
    () => slide.show_scale_bar ?? null
  )
  const [editShowMetadataPanel, setEditShowMetadataPanel] = useState<boolean | null>(
    () => slide.show_metadata_panel ?? null
  )
  // Image credit. Sent only when it differs from what was last seeded / saved
  // (creditBaselineRef), so an unrelated save never rewrites it.
  const [editCredit, setEditCredit] = useState<CreditForm>(() => creditFormFromSlide(slide))
  const creditBaselineRef = useRef<CreditForm>(trimCredit(creditFormFromSlide(slide)))
  // Image-adjustment preset, held as plain numbers (identity 1.0 = no preset
  // for that channel). Saved values that equal identity collapse to NULL on
  // the server; here they just sit at 1.0. "Clear preset" resets all to 1.
  const [editImageAdjust, setEditImageAdjust] = useState<ImageAdjust>(
    () => imageAdjustFromSlide(slide)
  )
  // True once the user has touched the preset sliders in this modal. The
  // fresh-fetch re-seed below skips re-seeding when set, so a slow response
  // can't silently reset in-progress slider edits. Reset on slide change.
  const presetTouchedRef = useRef(false)
  function updateImageAdjust(updater: (v: ImageAdjust) => ImageAdjust) {
    presetTouchedRef.current = true
    setEditImageAdjust(updater)
  }
  // The /api/admin/slides/{id}/{macro,label} endpoints require an admin Bearer
  // token, which a plain <img src=...> cannot carry. Fetch them through the
  // admin API helper, convert to blob URLs, and revoke when the slide changes
  // or the modal unmounts so DOM-attached blobs don't leak across edits.
  const [macroBlobUrl, setMacroBlobUrl] = useState<string>('')
  const [labelBlobUrl, setLabelBlobUrl] = useState<string>('')
  // Tracks which associated image is currently shown in the 1:1 zoom overlay.
  // Stores the blob URL plus presentation hints (label/background) rather than
  // an enum so the overlay doesn't need to re-read state from the parent and
  // works even if the user clicks rapidly through both images.
  const [zoomedImage, setZoomedImage] = useState<{ url: string; label: string; background: string } | null>(null)
  // Ref shadow so the Escape-key handler installed once on mount can consult
  // the current zoom state without re-binding the window listener every time
  // the user opens/closes the zoom overlay.
  const zoomedImageRef = useRef(zoomedImage)
  useEffect(() => { zoomedImageRef.current = zoomedImage }, [zoomedImage])
  // Clear any open zoom when switching to a different slide — the blob URL it
  // referenced is about to be revoked by the macro/label fetch effect.
  useEffect(() => { setZoomedImage(null) }, [slide.id])
  useEffect(() => {
    let revoked = false
    let macroUrl = ''
    let labelUrl = ''
    const ac = new AbortController()
    setMacroBlobUrl('')
    setLabelBlobUrl('')
    if (slide.macro_url) {
      adminFetchImageBlobUrl(slide.macro_url, { signal: ac.signal }).then(u => {
        if (revoked || !u) {
          if (u) URL.revokeObjectURL(u)
          return
        }
        macroUrl = u
        setMacroBlobUrl(u)
      }).catch(() => {})
    }
    if (slide.label_url) {
      adminFetchImageBlobUrl(slide.label_url, { signal: ac.signal }).then(u => {
        if (revoked || !u) {
          if (u) URL.revokeObjectURL(u)
          return
        }
        labelUrl = u
        setLabelBlobUrl(u)
      }).catch(() => {})
    }
    return () => {
      revoked = true
      ac.abort()
      if (macroUrl) URL.revokeObjectURL(macroUrl)
      if (labelUrl) URL.revokeObjectURL(labelUrl)
    }
  }, [slide.id, slide.macro_url, slide.label_url])

  useEffect(() => {
    // Re-seed the form when switching to a *different* slide (id change).
    // Same-slide reference updates (parent refreshes after save) are
    // intentionally skipped — the form already matches the persisted
    // values the user just submitted, and clobbering it would also wipe
    // the "saved" message.
    setEditMsg(null)
    setEditCatalogId(slide.catalog_id || '')
    setEditTitle(slide.title || '')
    storedTextRef.current = {
      catalogId: !!(slide.catalog_id || '').trim(),
      title: !!(slide.title || '').trim(),
    }
    metaBaselineRef.current = metaBaselineFromSlide(slide)
    const attrsMap: Record<string, string[]> = slide.attributes || {}
    const selections: Record<string, string[]> = {}
    Array.from(BASE_KEYS).forEach(k => { selections[k] = attrsMap[k] ? [...attrsMap[k]] : [] })
    Object.entries(attrsMap).forEach(([k, v]) => {
      if (BASE_KEYS.has(k)) return
      selections[k] = Array.isArray(v) ? [...v] : []
    })
    setEditSelections(selections)
    seedMpp(slide)
    setEditShowZoomIndicator(slide.show_zoom_indicator ?? null)
    setEditShowScaleBar(slide.show_scale_bar ?? null)
    setEditShowMetadataPanel(slide.show_metadata_panel ?? null)
    setEditCredit(creditFormFromSlide(slide))
    creditBaselineRef.current = trimCredit(creditFormFromSlide(slide))
    setEditImageAdjust(imageAdjustFromSlide(slide))
    presetTouchedRef.current = false
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slide.id])

  // The image-adjustment preset can also be changed from the viewer (the
  // in-viewer "Save as preset" button), which the dashboard's cached slide
  // list doesn't see until a full refresh. Re-seed the preset from a fresh
  // server fetch when the modal opens so it shows the current value — and so a
  // metadata save here can't silently overwrite a newer preset with a stale
  // one. Only the preset is re-seeded (the one field editable outside this
  // modal); other fields are left on their prop-seeded values so a slow fetch
  // never clobbers in-progress edits. The preset itself is also editable here,
  // so skip the re-seed once the user has started dragging the sliders.
  //
  // Save waits for that fetch (it sends the preset): a save before it lands
  // would write the cached one back. A failed fetch frees it as well.
  useEffect(() => {
    const ctrl = new AbortController()
    setPresetChecked(false)
    adminGetSlide(slide.id, { signal: ctrl.signal })
      .then((fresh) => {
        setPresetChecked(true)
        if (presetTouchedRef.current) return
        setEditImageAdjust(imageAdjustFromSlide(fresh))
      })
      .catch((err) => { if (!isAbortError(err)) setPresetChecked(true) /* keep prop-seeded preset */ })
    return () => ctrl.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slide.id])

  async function onDownloadWsi() {
    if (!slide.wsi_filename || wsiDownloading) return
    setWsiMsg(null)
    setWsiDownloading(true)
    try {
      await adminDownloadSlide(slide)
    } catch (err) {
      setWsiMsg({ text: apiErrorMessage(err, t) || t('adminDash.downloadWsiFailed'), tone: 'error' })
    } finally {
      setWsiDownloading(false)
    }
  }

  async function onWsiFileChange(ev: React.ChangeEvent<HTMLInputElement>) {
    const file = ev.target.files?.[0]
    ev.target.value = ''
    if (!file) return
    await replaceRequest.run(() => replaceWsi(file))
  }

  async function replaceWsi(file: File) {
    setWsiMsg(null)
    // Mirror the pre-checks the new-upload form runs (AdminUploadSection.tsx):
    // catch the wrong extension / oversize file before kicking off a multi-GB
    // streaming POST that the server would only reject after the body landed.
    // The server's cap (WSI_MAX_UPLOAD_BYTES) and extension list
    // (WSI_ALLOWED_EXTENSIONS), asked for now rather than assumed. If the
    // question fails, the default extensions apply and no size is refused
    // here: the server's own pre-body check answers 413 before the file is sent.
    let maxUploadBytes: number | null = null
    let serverExtensions: string[] | undefined
    try {
      const storage = await adminGetStorage()
      maxUploadBytes = storage.max_upload_bytes ?? null
      serverExtensions = storage.allowed_extensions
    } catch (err) {
      logger.error('storage info failed:', err)
    }
    const ext = `.${(file.name.split('.').pop() || '').toLowerCase()}`
    if (!acceptedWsiExtensions(serverExtensions).includes(ext)) {
      setWsiMsg({ text: t('adminDash.fileExtNotSupported', { ext }), tone: 'error' })
      return
    }
    if (maxUploadBytes != null && exceedsUploadLimit(file.size, maxUploadBytes)) {
      setWsiMsg({
        text: t('adminDash.fileSizeExceeded', { limit: formatByteLimit(maxUploadBytes) }),
        tone: 'error',
      })
      return
    }
    setWsiUploadProgress(0)
    setWsiStaging(false)
    // Whether the request body was fully transmitted. A transport failure after
    // that point does NOT mean the replace failed — the server may have applied
    // it and only the response was lost — so re-uploading a multi-GB file on
    // spec is the wrong advice. Mirrors uploadQueue's network-verify handling,
    // and like it is taken from onSent rather than a progress figure.
    let fullyUploaded = false
    try {
      await adminReplaceSlideWsi(slide.id, file, setWsiUploadProgress, {
        onSent: () => {
          fullyUploaded = true
          setWsiStaging(true)
        },
      })
      // The replacement has committed server-side at this point. Report success
      // BEFORE the list refresh so a transient failure of onSaved() (a network
      // blip or 429 right after a multi-GB upload) is not misreported as a
      // failed replace — which would prompt the operator to needlessly
      // re-upload the whole file. The refresh is best-effort; the next manual
      // refresh reconciles the list if it fails here.
      setWsiMsg({ text: t('adminDash.replaceWsiSuccess'), tone: 'success' })
      try {
        await onSaved()
      } catch {
        /* list refresh is best-effort; the replace already succeeded */
      }
      // The replace reset MPP to the new file's values; show those instead of
      // the pre-replace figures. If this fetch fails the fields keep the old
      // text, but it still equals the baseline, so a save leaves MPP alone.
      try {
        seedMpp(await adminGetSlide(slide.id))
      } catch {
        /* best-effort, see above */
      }
    } catch (err) {
      // Substitute localized text for the sentinel-message typed errors that
      // adminReplaceSlideWsi produces on 413 / 429; fall through to the
      // server detail for everything else.
      // A proxy's 502 / 504 / 52x after the body was sent (a read timeout
      // while the server validated the file) leaves the outcome as unknown
      // as a dropped connection does.
      if ((isNetworkError(err) || isGatewayError(err)) && fullyUploaded) {
        setWsiMsg({ text: t('adminDash.replaceWsiNetworkVerify'), tone: 'error' })
      } else if (isFileTooLargeError(err)) {
        setWsiMsg({
          text: maxUploadBytes != null
            ? t('common.fileTooLarge', { limit: formatByteLimit(maxUploadBytes) })
            : t('common.fileTooLargeServer'),
          tone: 'error',
        })
      } else if (isRateLimitError(err)) {
        setWsiMsg({ text: t('common.rateLimited'), tone: 'error' })
      } else {
        setWsiMsg({ text: apiErrorMessage(err, t) || t('adminDash.replaceWsiFailed'), tone: 'error' })
      }
    } finally {
      setWsiUploadProgress(null)
      setWsiStaging(false)
    }
  }

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    // Guard against a double submit (double-click / Enter twice): two
    // concurrent PATCH + list-refresh round-trips can trip the admin rate
    // limit and surface a spurious 429 on a save that actually succeeded.
    if (savingEdit || !presetChecked) return
    setEditMsg(null)
    const attrs: AttrMap = {}
    ;(Object.entries(editSelections) as [string, string[]][]).forEach(([k, vals]) => {
      const cleaned = (vals || []).filter(Boolean)
      if (cleaned.length) attrs[k] = cleaned
    })
    const metaErr = validateMetadata({ catalog_id: editCatalogId, title: editTitle }, attrs, t, metaBaselineRef.current)
    if (metaErr) { setEditMsg({ text: metaErr, tone: 'error' }); return }
    const mppXRaw = editMppX.trim()
    const mppYRaw = editMppSplit ? editMppY.trim() : mppXRaw
    const mppErr = validateMppInput(mppXRaw, editMppSplit ? t('adminDash.fieldMppX') : t('adminDash.fieldMpp'), t)
      || (editMppSplit ? validateMppInput(mppYRaw, t('adminDash.fieldMppY'), t) : null)
    if (mppErr) { setEditMsg({ text: mppErr, tone: 'error' }); return }
    // Not during a WSI replace: it resets MPP to the new file's, so the two
    // would undo each other in whichever order they land.
    const mppChanged = !uploadInFlight
      && (mppXRaw !== mppBaselineRef.current.x || mppYRaw !== mppBaselineRef.current.y)
    const mppSeedGen = mppSeedGenRef.current
    const credit = trimCredit(editCredit)
    const creditChanged = !sameCredit(credit, creditBaselineRef.current)
    // Only a credit about to be sent is checked: a stored one this check would
    // refuse (e.g. from an import) must not block saving unrelated fields.
    if (creditChanged) {
      // Counted in code points, as the server counts (not UTF-16 units).
      const textLen = [...credit.credit_text].length
      if (textLen > CREDIT_TEXT_MAX_LEN) {
        setEditMsg({ text: t('adminDash.validationMaxLen', { label: t('adminDash.fieldCreditText'), max: CREDIT_TEXT_MAX_LEN, len: textLen }), tone: 'error' })
        return
      }
      const urlLen = [...credit.source_url].length
      if (urlLen > SOURCE_URL_MAX_LEN) {
        setEditMsg({ text: t('adminDash.validationMaxLen', { label: t('adminDash.fieldSourceUrl'), max: SOURCE_URL_MAX_LEN, len: urlLen }), tone: 'error' })
        return
      }
      if (credit.source_url && !safeSourceUrl(credit.source_url)) {
        setEditMsg({ text: t('adminDash.apiErr.invalidSourceUrl'), tone: 'error' })
        return
      }
    }
    setSavingEdit(true)
    try {
      await adminUpdateSlide(slide.id, {
        // An empty form value reaches the backend as None ("field omitted"),
        // so a blanked catalog ID / title needs the explicit clear flag — sent
        // as-is it would report "saved" while keeping the old value. The
        // server still refuses to blank either on a published slide, so the
        // flag is only sent when a stored value is being blanked — a field
        // that was already blank is simply omitted (see storedTextRef).
        ...(editCatalogId.trim()
          ? { catalog_id: editCatalogId }
          : (storedTextRef.current.catalogId ? { clear_catalog_id: '1' } : {})),
        ...(editTitle.trim()
          ? { title: editTitle }
          : (storedTextRef.current.title ? { clear_title: '1' } : {})),
        attributes_json: JSON.stringify(attrs),
        // Blank means "drop the manual override and fall back to the value read
        // from the file", so it needs the same "null" sentinel as the tri-state
        // flags below — an empty form value reaches the backend as None, i.e.
        // "field omitted", and would leave the old override in place while the
        // dashboard reported a successful save. Untouched fields are omitted
        // (see mppBaselineRef).
        ...(mppChanged ? {
          mpp_x: mppXRaw === '' ? 'null' : mppXRaw,
          mpp_y: mppYRaw === '' ? 'null' : mppYRaw,
        } : {}),
        // Send the literal string "null" for the cleared/no-override state
        // rather than an empty string. FastAPI / python-multipart treat
        // empty form values as missing for `Optional[str] = Form(None)`,
        // which makes the backend's `if x is not None` guard skip the
        // update — leaving the previous DB value in place. The backend's
        // _parse_tri_bool already recognises "null" → None.
        show_zoom_indicator: editShowZoomIndicator === null ? 'null' : (editShowZoomIndicator ? '1' : '0'),
        show_scale_bar:      editShowScaleBar      === null ? 'null' : (editShowScaleBar      ? '1' : '0'),
        show_metadata_panel: editShowMetadataPanel === null ? 'null' : (editShowMetadataPanel ? '1' : '0'),
        // Image-adjustment preset. The backend clamps each channel and
        // collapses identity (1.0) to NULL, so sending 1 for an untouched
        // channel clears that part of the preset.
        image_adjust_brightness: String(editImageAdjust.brightness),
        image_adjust_contrast:   String(editImageAdjust.contrast),
        image_adjust_saturation: String(editImageAdjust.saturation),
        // One object replacing all three credit fields (a blank field is just
        // "" inside it, so no clear_* flags are needed).
        ...(creditChanged ? { credit_json: JSON.stringify(credit) } : {}),
      })
      // Refresh BEFORE showing "saved" so the parent's slides list reflects
      // the persisted state by the time the user can interact again.
      // Without this, closing and reopening the modal during the refresh
      // round-trip would show stale data and make the change look like it
      // wasn't saved.
      if (mppSeedGenRef.current === mppSeedGen) mppBaselineRef.current = { x: mppXRaw, y: mppYRaw }
      creditBaselineRef.current = credit
      storedTextRef.current = { catalogId: !!editCatalogId.trim(), title: !!editTitle.trim() }
      metaBaselineRef.current = { fields: { catalog_id: editCatalogId, title: editTitle }, attrs }
      // The PATCH has committed, so the refresh is best-effort: a transient
      // 429 / 5xx here must not be reported as a failed save.
      try {
        await onSaved()
      } catch (refreshErr) {
        logger.error('slide list refresh after save failed:', refreshErr)
      }
      setEditMsg({ text: t('adminDash.saved'), tone: 'success' })
    } catch (err) {
      // Preserve the server-side reason (e.g. duplicate filename, WSI
      // unreadable) instead of swallowing it into a generic "Save failed".
      setEditMsg({ text: failureMessage(t('adminDash.saveFailed'), err, t), tone: 'error' })
    } finally {
      setSavingEdit(false)
    }
  }

  const fx = slide.mpp_x_file || 0
  const fy = slide.mpp_y_file || 0
  const cx = slide.mpp_x || 0
  const cy = slide.mpp_y || 0
  const hasFile = fx > 0 || fy > 0
  const overridden = hasFile && (Math.abs(cx - fx) > 1e-9 || Math.abs(cy - fy) > 1e-9)
  const ext = slide.wsi_filename?.match(/\.([^.]+)$/)?.[1]?.toUpperCase() ?? ''

  // Closing the modal mid-WSI-upload would drop progress feedback and leave
  // the user without a way to see the eventual success/error (the XHR
  // continues server-side regardless because of the parking/swap rollback
  // logic, so suppressing the close — rather than aborting — is the safe
  // choice). All three close affordances (Escape, backdrop click, Close
  // button) consult this guard.
  const uploadInFlight = replaceRequest.busy || wsiUploadProgress !== null
  const requestClose = () => { if (!uploadInFlight) onClose() }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // When the 1:1 zoom overlay is open, Escape should dismiss the overlay
      // first instead of tearing down the whole edit modal underneath it.
      if (zoomedImageRef.current) { setZoomedImage(null); return }
      requestClose()
    }
    window.addEventListener('keydown', onKey)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uploadInFlight, onClose])

  // The zoom overlay, while open, keeps focus to itself; closing it returns
  // focus to the image that opened it.
  const panelRef = useRef<HTMLDivElement | null>(null)
  const zoomRef = useRef<HTMLDivElement | null>(null)
  useDialogFocus(panelRef)
  useDialogFocus(zoomRef, zoomedImage !== null)

  return createPortal((
    <div
      className="overscroll-panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="slide-edit-title"
      onClick={requestClose}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0, 0, 0, 0.45)',
        zIndex: 'var(--z-modal, 1000)' as unknown as number,
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        padding: 20,
        overflowY: 'auto',
      }}
    >
    <div
      ref={panelRef}
      tabIndex={-1}
      onClick={e => e.stopPropagation()}
      style={{
        background: 'var(--color-surface, #fff)',
        border: '1px solid var(--color-border)',
        borderRadius: 12,
        padding: 12,
        width: '100%',
        maxWidth: 720,
        boxShadow: 'var(--shadow-lg, 0 10px 30px rgba(0,0,0,0.25))',
        margin: 'auto',
      }}
    >
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <div id="slide-edit-title" style={{ fontWeight: 800 }}>{t('adminDash.editMetadataTitle', { id: slide.id })}</div>
        <button
          style={{ marginLeft: 'auto' }}
          onClick={requestClose}
          disabled={uploadInFlight}
          title={uploadInFlight ? t('adminDash.replaceWsiUploading') : undefined}
        >
          {t('adminDash.close')}
        </button>
      </div>
      <form onSubmit={onSubmit} style={{ display: 'grid', gap: 8, marginTop: 10, gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
        <label style={{ gridColumn: '1 / -1', display: 'flex', flexDirection: 'column', gap: 2 }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.fieldCatalogId')}</span>
          <input name="catalog_id" value={editCatalogId} onChange={e => setEditCatalogId(e.target.value)} placeholder={t('adminDash.placeholderCatalogId')} style={{ boxSizing: 'border-box', width: '100%' }} />
        </label>
        <label style={{ gridColumn: '1 / -1', display: 'flex', flexDirection: 'column', gap: 2 }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.fieldTitle')}</span>
          <input name="title" value={editTitle} onChange={e => setEditTitle(e.target.value)} placeholder="" style={{ boxSizing: 'border-box', width: '100%' }} />
        </label>
        <div style={{ gridColumn: '1 / -1' }}>
          <OptionMultiSelect label={t('adminDash.fieldStain')} k="stain" options={metaOptions['stain'] || []} selected={editSelections['stain'] || []} onChange={vals => setEditSelections(s => ({ ...s, stain: vals }))} t={t} />
        </div>
        <div style={{ gridColumn: '1 / -1' }}>
          <OptionMultiSelect label={t('adminDash.fieldOrgan')} k="organ" options={metaOptions['organ'] || []} selected={editSelections['organ'] || []} onChange={vals => setEditSelections(s => ({ ...s, organ: vals }))} t={t} />
        </div>
        <div style={{ gridColumn: '1 / -1' }}>
          <OptionMultiSelect label={t('adminDash.fieldLab')} k="lab" options={metaOptions['lab'] || []} selected={editSelections['lab'] || []} onChange={vals => setEditSelections(s => ({ ...s, lab: vals }))} t={t} />
        </div>
        <div style={{ gridColumn: '1 / -1' }}>
          <OptionMultiSelect label={t('adminDash.fieldTags')} k="tags" options={metaOptions['tags'] || []} selected={editSelections['tags'] || []} onChange={vals => setEditSelections(s => ({ ...s, tags: vals }))} t={t} />
        </div>
        {(() => {
          // Render OptionMultiSelect for every custom category registered in
          // the master list, plus any extra key the slide carries that is
          // *not* in the master list. Without this, those orphan keys
          // would be invisible in the modal yet preserved on save
          // (editSelections still includes them), so admins would have no
          // UI affordance to remove them.
          const knownCustom = new Set(customMetaKeys)
          const orphanKeys: string[] = []
          for (const k of Object.keys(editSelections)) {
            if (BASE_KEYS.has(k)) continue
            if (knownCustom.has(k)) continue
            if ((editSelections[k] || []).length === 0) continue
            orphanKeys.push(k)
          }
          orphanKeys.sort()
          return [...customMetaKeys, ...orphanKeys].map((k) => (
            <div key={k} style={{ gridColumn: '1 / -1' }}>
              <OptionMultiSelect label={k} k={k} options={metaOptions[k] || []} selected={editSelections[k] || []} onChange={vals => setEditSelections(s => ({ ...s, [k]: vals }))} t={t} />
            </div>
          ))
        })()}

        {/* Image credit section */}
        <div style={{ gridColumn: '1 / -1', border: '1px dashed var(--color-border-strong)', borderRadius: 8, padding: 8 }}>
          <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em', opacity: 0.55, marginBottom: 6 }}>
            {t('adminDash.creditSection')}
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8 }}>
            {t('adminDash.creditHelp')}
          </div>
          <div style={{ display: 'grid', gap: 8 }}>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.fieldCreditText')}</span>
              <input
                name="credit_text"
                list="slide-credit-suggestions"
                value={editCredit.credit_text}
                onChange={e => setEditCredit(c => ({ ...c, credit_text: e.target.value }))}
                placeholder={t('adminDash.placeholderCreditText')}
                style={{ boxSizing: 'border-box', width: '100%' }}
              />
              <datalist id="slide-credit-suggestions">
                {creditSuggestions.map(v => <option key={v} value={v} />)}
              </datalist>
            </label>
            <div style={{ display: 'grid', gap: 8, gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 2fr)' }}>
              <label style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.fieldLicense')}</span>
                <select
                  name="license_id"
                  value={editCredit.license_id}
                  onChange={e => setEditCredit(c => ({ ...c, license_id: e.target.value }))}
                  style={{ boxSizing: 'border-box', width: '100%' }}
                >
                  <option value="">{t('adminDash.licenseNone')}</option>
                  {Object.keys(SLIDE_LICENSES).map(id => (
                    <option key={id} value={id}>{licenseLabel(id, t)}</option>
                  ))}
                  {/* A stored id this build does not list (a hand-edited row, or a
                      stale cached page talking to a newer server — an import
                      drops unknown ids) stays selectable, so editing another
                      credit field does not silently drop it; the server still
                      decides whether the id is valid. */}
                  {editCredit.license_id && !SLIDE_LICENSES[editCredit.license_id] && (
                    <option value={editCredit.license_id}>{editCredit.license_id}</option>
                  )}
                </select>
              </label>
              <label style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>{t('adminDash.fieldSourceUrl')}</span>
                <input
                  name="source_url"
                  inputMode="url"
                  value={editCredit.source_url}
                  maxLength={SOURCE_URL_MAX_LEN}
                  onChange={e => setEditCredit(c => ({ ...c, source_url: e.target.value }))}
                  placeholder="https://"
                  style={{ boxSizing: 'border-box', width: '100%' }}
                />
              </label>
            </div>
            {(editCredit.credit_text.trim() || editCredit.license_id || editCredit.source_url.trim()) && (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 8, padding: 8, borderRadius: 6, background: 'var(--color-text-muted, #666)' }}>
                <span style={{ fontSize: 10, color: '#fff', opacity: 0.8, marginRight: 'auto' }}>{t('adminDash.creditPreview')}</span>
                <SlideCredit credit={trimCredit(editCredit)} />
              </div>
            )}
          </div>
        </div>

        {/* File info section */}
        <div style={{ gridColumn: '1 / -1', border: '1px dashed var(--color-border-strong)', borderRadius: 8, padding: 8 }}>
          <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em', opacity: 0.55, marginBottom: 6 }}>
            {t('adminDash.cardSectionFileInfo')}
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8 }}>
            {t('adminDash.fileInfoHelp')}
          </div>
          <dl style={{ display: 'grid', gap: 4, gridTemplateColumns: 'max-content 1fr', columnGap: 12, margin: 0, fontSize: 12 }}>
            {slide.wsi_filename && (
              <>
                <dt style={{ color: 'var(--color-text-muted)' }}>{t('adminDash.slideFilename')}</dt>
                <dd style={{ margin: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }} title={slide.wsi_filename}>{displayFilename(slide.wsi_filename)}</dd>
              </>
            )}
            <dt style={{ color: 'var(--color-text-muted)' }}>{t('adminDash.slideFileSize')}</dt>
            <dd style={{ margin: 0 }}>{formatBytes(slide.file_size_bytes || null)}</dd>
            {ext && (
              <>
                <dt style={{ color: 'var(--color-text-muted)' }}>{t('adminDash.slideExt')}</dt>
                <dd style={{ margin: 0 }}>{ext}</dd>
              </>
            )}
            <dt style={{ color: 'var(--color-text-muted)' }}>{t('adminDash.slideSize')}</dt>
            <dd style={{ margin: 0 }}>
              {(slide.width > 0 || slide.height > 0)
                ? `${Number(slide.width).toLocaleString()} × ${Number(slide.height).toLocaleString()} px`
                : '—'}
            </dd>
            <dt style={{ color: 'var(--color-text-muted)' }}>{t('adminDash.slideVendor')}</dt>
            <dd style={{ margin: 0 }}>{slide.vendor || '—'}</dd>
            <dt style={{ color: 'var(--color-text-muted)' }}>{t('adminDash.slideFileMpp')}</dt>
            <dd style={{ margin: 0 }}>
              {hasFile ? formatMpp(fx, fy) : '—'}
              {overridden && (
                <span
                  title={t('adminDash.mppOverriddenTitle', { current: formatMpp(cx, cy) })}
                  style={{ marginLeft: 6, fontSize: 10, fontWeight: 700, padding: '1px 6px', borderRadius: 4, background: 'var(--color-warning-bg, #fef3c7)', color: 'var(--color-warning-fg, #92400e)', whiteSpace: 'nowrap' }}
                >
                  {t('adminDash.mppOverridden')}
                </span>
              )}
            </dd>
            <dt style={{ color: 'var(--color-text-muted)' }}>{t('adminDash.slideFileMagnification')}</dt>
            <dd style={{ margin: 0 }}>
              {(() => {
                // Prefer the magnification recorded in the WSI metadata
                // (openslide.objective-power, surfaced as objective_power_file)
                // when the file carries one. Fall back to the MPP-derived
                // estimate (10 / mpp_x) only when metadata is absent.
                const op = slide.objective_power_file ?? 0
                if (op > 0) {
                  const rounded = Number.isInteger(op) ? op : Math.round(op * 10) / 10
                  return `${rounded}×`
                }
                return fx > 0 ? `≈ ${Math.round(10 / fx)}×` : '—'
              })()}
            </dd>
            <dt style={{ color: 'var(--color-text-muted)' }}>{t('adminDash.slideAcquiredAt')}</dt>
            <dd style={{ margin: 0 }}>{formatAcquiredAt(slide.acquired_at)}</dd>
            <dt style={{ color: 'var(--color-text-muted)' }}>{t('adminDash.fieldCreatedAt')}</dt>
            <dd style={{ margin: 0 }}>{formatInEmbeddedOffset(slide.created_at)}</dd>
          </dl>

          {(slide.macro_url || slide.label_url) && (
            <div style={{ marginTop: 10, paddingTop: 8, borderTop: '1px solid var(--color-border)' }}>
              <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)', marginBottom: 6 }}>
                {t('adminDash.associatedImagesTitle')}
              </div>
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                {slide.macro_url && macroBlobUrl && (
                  <figure style={{ margin: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
                    <button
                      type="button"
                      onClick={() => setZoomedImage({ url: macroBlobUrl, label: t('adminDash.macroImageLabel'), background: '#000' })}
                      title={t('adminDash.associatedImageZoomHint')}
                      aria-label={t('adminDash.associatedImageZoomHint')}
                      style={{ padding: 0, border: 0, background: 'transparent', cursor: 'zoom-in', display: 'block' }}
                    >
                      <img
                        src={macroBlobUrl}
                        alt={t('adminDash.macroImageLabel')}
                        style={{ display: 'block', maxHeight: 140, maxWidth: 240, objectFit: 'contain', border: '1px solid var(--color-border)', borderRadius: 4, background: '#000' }}
                      />
                    </button>
                    <figcaption style={{ fontSize: 10, color: 'var(--color-text-muted)' }}>{t('adminDash.macroImageLabel')}</figcaption>
                  </figure>
                )}
                {slide.label_url && labelBlobUrl && (
                  <figure style={{ margin: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
                    <button
                      type="button"
                      onClick={() => setZoomedImage({ url: labelBlobUrl, label: t('adminDash.labelImageLabel'), background: '#fff' })}
                      title={t('adminDash.associatedImageZoomHint')}
                      aria-label={t('adminDash.associatedImageZoomHint')}
                      style={{ padding: 0, border: 0, background: 'transparent', cursor: 'zoom-in', display: 'block' }}
                    >
                      <img
                        src={labelBlobUrl}
                        alt={t('adminDash.labelImageLabel')}
                        style={{ display: 'block', maxHeight: 140, maxWidth: 240, objectFit: 'contain', border: '1px solid var(--color-border)', borderRadius: 4, background: '#fff' }}
                      />
                    </button>
                    <figcaption style={{ fontSize: 10, color: 'var(--color-text-muted)' }}>
                      {t('adminDash.labelImageLabel')}
                      <span style={{ marginLeft: 4, opacity: 0.75 }}>· {t('adminDash.labelImageAdminOnly')}</span>
                    </figcaption>
                  </figure>
                )}
              </div>
            </div>
          )}

          {/* WSI replace / attach */}
          <div style={{ marginTop: 10, paddingTop: 8, borderTop: '1px solid var(--color-border)' }}>
            <div style={{ fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 6 }}>
              {t('adminDash.replaceWsiHint')}
            </div>
            <input
              ref={wsiFileRef}
              type="file"
              accept={ACCEPTED_WSI_EXTENSIONS.join(',')}
              style={{ display: 'none' }}
              onChange={onWsiFileChange}
            />
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <button
                type="button"
                disabled={uploadInFlight}
                onClick={() => { setWsiMsg(null); wsiFileRef.current?.click() }}
                style={{ fontSize: 12 }}
              >
                {wsiUploadProgress !== null
                  ? (wsiStaging
                      ? t('adminDash.replaceWsiProcessing')
                      : `${t('adminDash.replaceWsiUploading')} ${wsiUploadProgress}%`)
                  : (slide.wsi_filename ? t('adminDash.replaceWsi') : t('adminDash.attachWsi'))}
              </button>
              {/* Original-file download is admin-only (POST .../download_url is
                  require_admin); a manager would only ever get a 403. */}
              {slide.wsi_filename && canDownloadWsi && (
                <button
                  type="button"
                  disabled={wsiDownloading || uploadInFlight}
                  onClick={onDownloadWsi}
                  style={{ fontSize: 12 }}
                >
                  {wsiDownloading ? t('adminDash.downloadWsiPreparing') : t('adminDash.downloadWsi')}
                </button>
              )}
            </div>
            {wsiMsg && (
              <div style={{
                marginTop: 4, fontSize: 11,
                color: wsiMsg.tone === 'success'
                  ? 'var(--color-success-fg, #1a7f37)'
                  : 'var(--color-danger-fg, #b00020)',
              }}>
                {wsiMsg.text}
              </div>
            )}
          </div>
        </div>

        {/* MPP section */}
        {/* Locked while a WSI replace is in flight: it ends by showing the new
            file's MPP, which would overwrite a value typed meanwhile. */}
        <LockFieldset
          locked={uploadInFlight}
          style={{ gridColumn: '1 / -1', border: '1px dashed var(--color-border-strong)', borderRadius: 8, padding: 8 }}
        >
          <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em', opacity: 0.55, marginBottom: 6 }}>
            {t('adminDash.mppSection')}
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8 }}>
            {t('adminDash.mppHelp')}
          </div>
          {!editMppSplit ? (
            <label style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>MPP (μm/px)</span>
              <input
                name="mpp_x"
                type="number"
                step="0.0001"
                min="0"
                value={editMppX}
                onChange={e => setEditMppX(e.target.value)}
                placeholder={t('adminDash.placeholderMpp')}
                style={{ boxSizing: 'border-box', width: '100%' }}
              />
            </label>
          ) : (
            <div style={{ display: 'grid', gap: 8, gridTemplateColumns: '1fr 1fr' }}>
              <label style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>MPP X (μm/px)</span>
                <input
                  name="mpp_x"
                  type="number"
                  step="0.0001"
                  min="0"
                  value={editMppX}
                  onChange={e => setEditMppX(e.target.value)}
                  placeholder={t('adminDash.placeholderMpp')}
                  style={{ boxSizing: 'border-box', width: '100%' }}
                />
              </label>
              <label style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-text-muted)' }}>MPP Y (μm/px)</span>
                <input
                  name="mpp_y"
                  type="number"
                  step="0.0001"
                  min="0"
                  value={editMppY}
                  onChange={e => setEditMppY(e.target.value)}
                  placeholder={t('adminDash.placeholderMpp')}
                  style={{ boxSizing: 'border-box', width: '100%' }}
                />
              </label>
            </div>
          )}
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8, fontSize: 11, color: 'var(--color-text-muted)', cursor: 'pointer' }}>
            <input
              name="mpp_split"
              type="checkbox"
              checked={editMppSplit}
              onChange={e => {
                const next = e.target.checked
                setEditMppSplit(next)
                // When switching to split mode, seed Y from X so the user only
                // has to tweak the axis that differs.
                if (next && !editMppY.trim()) setEditMppY(editMppX)
              }}
            />
            {t('adminDash.mppSplit')}
          </label>
        </LockFieldset>

        {/* Viewer overlay overrides section */}
        <div style={{ gridColumn: '1 / -1', border: '1px dashed var(--color-border-strong)', borderRadius: 8, padding: 8 }}>
          <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em', opacity: 0.55, marginBottom: 6 }}>
            {t('adminDash.viewerOverlaySection')}
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8 }}>
            {t('adminDash.viewerOverlayHelp')}
          </div>
          {!globalOverlays && (
            <div style={{ fontSize: 11, color: 'var(--color-warning-fg, #92400e)', marginBottom: 8 }}>
              {t('adminDash.viewerOverlayGlobalUnknown')}
            </div>
          )}
          <div style={{ display: 'grid', gap: 6 }}>
            <OverlayOverrideRow
              label={t('adminDash.zoomIndicator')}
              globalValue={globalOverlays?.showZoomIndicator ?? null}
              perSlide={editShowZoomIndicator}
              onChange={setEditShowZoomIndicator}
              overrideLabel={t('adminDash.overrideLabel')}
              overrideTitleFn={(gv, sv) => t('adminDash.overrideTitle', { label: t('adminDash.zoomIndicator'), globalState: gv ? t('adminDash.globalShow') : t('adminDash.globalHide'), slideState: sv ? t('adminDash.globalShow') : t('adminDash.globalHide') })}
              resetTitle={t('adminDash.overrideReset')}
            />
            <OverlayOverrideRow
              label={t('adminDash.scaleBar')}
              globalValue={globalOverlays?.showScaleBar ?? null}
              perSlide={editShowScaleBar}
              onChange={setEditShowScaleBar}
              overrideLabel={t('adminDash.overrideLabel')}
              overrideTitleFn={(gv, sv) => t('adminDash.overrideTitle', { label: t('adminDash.scaleBar'), globalState: gv ? t('adminDash.globalShow') : t('adminDash.globalHide'), slideState: sv ? t('adminDash.globalShow') : t('adminDash.globalHide') })}
              resetTitle={t('adminDash.overrideReset')}
            />
            <OverlayOverrideRow
              label={t('adminDash.metadataPanel')}
              globalValue={globalOverlays?.showMetadataPanel ?? null}
              perSlide={editShowMetadataPanel}
              onChange={setEditShowMetadataPanel}
              overrideLabel={t('adminDash.overrideLabel')}
              overrideTitleFn={(gv, sv) => t('adminDash.overrideTitle', { label: t('adminDash.metadataPanel'), globalState: gv ? t('adminDash.globalShow') : t('adminDash.globalHide'), slideState: sv ? t('adminDash.globalShow') : t('adminDash.globalHide') })}
              resetTitle={t('adminDash.overrideReset')}
            />
          </div>
        </div>

        {/* Image-adjustment preset section */}
        <div style={{ gridColumn: '1 / -1', border: '1px dashed var(--color-border-strong)', borderRadius: 8, padding: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginBottom: 6 }}>
            <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.06em', opacity: 0.55 }}>
              {t('adminDash.imageAdjustSection')}
            </div>
            <button
              type="button"
              disabled={isImageAdjustDefault(editImageAdjust)}
              onClick={() => updateImageAdjust(() => ({ brightness: 1, contrast: 1, saturation: 1 }))}
              style={{
                cursor: isImageAdjustDefault(editImageAdjust) ? 'default' : 'pointer',
                background: 'none',
                border: '1px solid var(--color-border-strong, #aaa)',
                borderRadius: 4,
                fontSize: 11,
                lineHeight: 1,
                padding: '3px 8px',
                color: 'var(--color-text-muted)',
                opacity: isImageAdjustDefault(editImageAdjust) ? 0.5 : 1,
              }}
            >
              {t('adminDash.imageAdjustClear')}
            </button>
          </div>
          <div style={{ fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8 }}>
            {t('adminDash.imageAdjustHelp')}
          </div>
          <div style={{ display: 'grid', gap: 6, maxWidth: 320 }}>
            <AdjustSliderRow
              label={t('viewer.adjust.brightness')}
              value={editImageAdjust.brightness}
              range={IMAGE_ADJUST_RANGES.brightness}
              onChange={(n) => updateImageAdjust(v => ({ ...v, brightness: n }))}
            />
            <AdjustSliderRow
              label={t('viewer.adjust.contrast')}
              value={editImageAdjust.contrast}
              range={IMAGE_ADJUST_RANGES.contrast}
              onChange={(n) => updateImageAdjust(v => ({ ...v, contrast: n }))}
            />
            <AdjustSliderRow
              label={t('viewer.adjust.saturation')}
              value={editImageAdjust.saturation}
              range={IMAGE_ADJUST_RANGES.saturation}
              onChange={(n) => updateImageAdjust(v => ({ ...v, saturation: n }))}
            />
          </div>
        </div>

        {/* Keep the feedback message immediately above the submit
            button (and inside the form's grid). For long forms the
            submit button is the last thing on screen when the user
            clicks Save; placing the message here means the post-save
            text appears right where the click landed, instead of
            below the button — which on smaller laptop viewports can
            push it outside the modal's visible area. */}
        {editMsg && (
          <div style={{ gridColumn: '1 / -1', fontSize: 12, whiteSpace: 'pre-line', color: editMsg.tone === 'success' ? 'var(--color-success-fg)' : 'var(--color-danger-fg)' }}>
            {editMsg.text}
          </div>
        )}
        <button type="submit" disabled={savingEdit || !presetChecked} style={{ gridColumn: '1 / -1' }}>{savingEdit ? t('adminDash.saving') : t('adminDash.save')}</button>
      </form>
    </div>
    {zoomedImage && (
      // Portaled inside the edit-modal subtree, so React event bubbling reaches
      // the outer overlay's onClick. The stopPropagation calls below prevent
      // clicks on the zoom backdrop / image from also triggering requestClose
      // on the edit modal underneath. The overlay is fixed-positioned and
      // sits above the edit modal via a higher z-index.
      <div
        ref={zoomRef}
        className="overscroll-panel"
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={zoomedImage.label}
        onClick={(e) => { e.stopPropagation(); setZoomedImage(null) }}
        style={{
          position: 'fixed',
          inset: 0,
          background: 'rgba(0, 0, 0, 0.85)',
          zIndex: 'calc(var(--z-modal, 1000) + 10)' as unknown as number,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: 20,
          overflow: 'auto',
        }}
      >
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); setZoomedImage(null) }}
          style={{ position: 'fixed', top: 12, right: 12, zIndex: 1 }}
        >
          {t('adminDash.associatedImageZoomClose')}
        </button>
        <img
          src={zoomedImage.url}
          alt={zoomedImage.label}
          onClick={(e) => e.stopPropagation()}
          style={{ display: 'block', background: zoomedImage.background, boxShadow: '0 4px 24px rgba(0,0,0,0.6)' }}
        />
      </div>
    )}
    </div>
  ), document.body)
}
