import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import SafeMarkdown from '../components/SafeMarkdown'
import ImageAdjustControls from '../components/ImageAdjustControls'
import ConflictBanner from '../components/admin/ConflictBanner'
import DebugToggle from '../components/DebugToggle'
import MouseCoordsOverlay from '../components/MouseCoordsOverlay'
import TourAssetsPanel from '../components/admin/TourAssetsPanel'
import TourPublicUrl from '../components/admin/TourPublicUrl'
import TourStepEditForm from '../components/admin/TourStepEditForm'
import TourMetadataModal from '../components/admin/TourMetadataModal'
import { LinkButton, VisibilitySelect } from '../components/ui'
import { Visibility, VisibilityError, applyVisibility, visibilityOf } from '../lib/visibility'
import { apiErrorMessage } from '../lib/admin/apiError'
import { useTourViewer } from '../hooks/useTourViewer'
import { useDebugParam } from '../hooks/useDebugParam'
import { useMediaQuery } from '../hooks/useMediaQuery'
import { useUnsavedNavigationGuard } from '../hooks/useUnsavedNavigationGuard'
import { useViewportHeight } from '../hooks/useViewportHeight'
import { useInFlight } from '../hooks/useInFlight'
import { TOUR_EDITOR_STACK_QUERY, tourEditorLayout } from '../lib/tourEditorLayout'
import { useI18n } from '../lib/i18n'
import {
  DEFAULT_IMAGE_ADJUST,
  ImageAdjust,
  cssFilterString,
  imageAdjustFromSlide,
} from '../lib/imageAdjust'
import {
  PinItem,
  PinShape,
  Shape,
  Slide,
  TourDetail,
  TourSummary,
  adminAddStep,
  adminAppendStepPin,
  adminAppendStepShape,
  adminDeleteStep,
  adminDeleteTour,
  adminGetSlide,
  adminGetTour,
  adminListTourAssets,
  adminPreviewLinkProps,
  adminReorderSteps,
  adminSaveSlideImageAdjust,
  adminUpdateStep,
  adminUpdateTour,
  isAbortError,
  isApiCodeError,
  isManagerOrAboveFromToken,
  isUnauthorizedError,
  normalizeTransitionMode,
  stepConflictOf,
  tourConflictOf,
  tourVisibilityApi,
  DEFAULT_TRANSITION_MODE,
} from '../lib/api'
import { DEFAULT_PIN_SHAPE, normalizePinShape, normalizeRotationDeg } from '../lib/tourPins'
import { MAX_FREEHAND_POINTS, findOverCapShape, normalizeShape } from '../lib/tourShapes'
import { LAYER_PIN_BASE, nextPinZ } from '../lib/tourLayers'
import { fovFromPhysical, fovToPhysical } from '../lib/tourStepView'
import { slidePixelSize } from '../lib/scalebar'
import {
  type EditState,
  type Step,
  type StepSaveResult,
  applyStepOrder,
  makePinId,
  makeShapeId,
  mergeStep,
  resolveStepSave,
  stepPins,
} from '../lib/tourEditHelpers'

export default function AdminTourEditor() {
  const { t } = useI18n()
  const { tourId } = useParams()
  const tid = Number(tourId)
  const nav = useNavigate()

  const [debugMode, toggleDebug] = useDebugParam()

  const [tour, setTour] = useState<TourSummary | null>(null)
  const [slide, setSlide] = useState<Slide | null>(null)
  const [steps, setSteps] = useState<Step[]>([])
  const [msg, setMsg] = useState('')
  const [assets, setAssets] = useState<{ name: string; url: string }[]>([])
  // The server's per-file tour-asset cap, from the asset listing.
  const [assetMaxBytes, setAssetMaxBytes] = useState<number | null>(null)
  const [editingTitle, setEditingTitle] = useState<string | null>(null)
  // A title save is in flight. The input is read-only meanwhile: the save
  // closes the editor on success, and anything typed after the request went
  // out would be dropped with it. The ref guards a second Enter that lands
  // before the state does.
  const [titleSaving, setTitleSaving] = useState(false)
  const titleSavingRef = useRef(false)
  const [editingStepId, setEditingStepId] = useState<number | null>(null)
  const [editState, setEditState] = useState<EditState | null>(null)
  const [selectedStepId, setSelectedStepId] = useState<number | null>(null)
  const [pendingPinShape, setPendingPinShape] = useState<PinShape | null>(null)
  // Whether the top-left nav overlay (dashboard link, title, visibility,
  // preview, delete, public URL) is expanded. Default expanded so the controls
  // are discoverable; collapsing shrinks it to a small chip so the operator can
  // see the top-left of the WSI unobstructed. Manual toggle only — it does NOT
  // auto-collapse on canvas clicks (the operator clicks the slide constantly).
  const [navOpen, setNavOpen] = useState(true)
  // Image adjustment (brightness/contrast/saturation) for the editor preview.
  // Seeded from the slide's saved preset so the editor shows the same look the
  // public viewer will; managers can retune and persist it back as the preset.
  const [imageAdjust, setImageAdjust] = useState<ImageAdjust>(DEFAULT_IMAGE_ADJUST)
  // Guards the create-step operations (add / duplicate) against double-submit.
  // Both compute their order from the current `steps` snapshot, so a second
  // click while the first request is still in flight would derive the same
  // order and land two steps at the same position (or trip a 409 on reorder).
  const [stepBusy, setStepBusy] = useState(false)
  // Guards the two-request applyVisibility sequence so rapid clicks can't
  // interleave runs computed from stale tour flags (which would persist a
  // visibility state contradicting the admin's last click).
  const [visBusy, setVisBusy] = useState(false)
  // Deleting the tour: everything else waits for it (it leaves the page).
  const deleteTourRequest = useInFlight()
  // Every write that sends the tour revision (a step add / duplicate / move /
  // delete, the visibility, the title, the tour's metadata) waits for the
  // others: two out at once would refuse the second as "changed elsewhere".
  const tourWriteBusy = stepBusy || visBusy || titleSaving || deleteTourRequest.busy
  // Snapshot of the editor state that matches what is saved: taken on
  // startEdit, and replaced by the submitted state when a save lands while
  // newer edits are still open (see saveEdit). Used to detect unsaved changes
  // and confirm before discarding them. JSON-stringified once (instead of a
  // structural compare every render) and fed into the `isDirty` memo below,
  // which drives confirmDiscardIfDirty (Cancel, the dashboard link, switching
  // to edit another step) plus the beforeunload guard.
  const [editBaseline, setEditBaseline] = useState<string | null>(null)
  // Id of the step whose save request is in flight, or null. Disables that
  // editor's Save button and every other step's Edit button until the
  // response lands, so one save at a time is outstanding and the editor
  // can't be retargeted underneath it.
  const [savingStepId, setSavingStepId] = useState<number | null>(null)
  // Live mirrors for saveEdit's continuation, which otherwise only sees the
  // state captured when the save started. Layout effects so they are current
  // before any network callback can run after a commit.
  const editStateRef = useRef<EditState | null>(null)
  const editingStepIdRef = useRef<number | null>(null)
  useLayoutEffect(() => { editStateRef.current = editState }, [editState])
  useLayoutEffect(() => { editingStepIdRef.current = editingStepId }, [editingStepId])
  // The step revision the open editor was loaded from (startEdit) or last
  // saved at (saveEdit), sent with every save as expected_revision: the
  // server refuses (409) a save built on a version of the step this editor
  // never saw — one saved from another window, by another admin, or an
  // append from "Copy to next step" — instead of silently replacing it.
  // null = unknown (sends none: unconditional).
  const [editRevision, setEditRevision] = useState<number | null>(null)
  // The server's current copy of the open step after a save was refused for
  // that reason. While set, the editor keeps the user's edits and offers
  // "Reload (discard my changes)" / "Overwrite with mine".
  const [stepConflict, setStepConflict] = useState<Step | null>(null)
  // The tour's current title after a title save was refused because the tour
  // changed elsewhere (shown beside the still-open title input).
  const [titleConflict, setTitleConflict] = useState<string | null>(null)
  // Whether the tour metadata dialog is open (it guards its own unsaved
  // changes: closing it asks first).
  const [metadataOpen, setMetadataOpen] = useState(false)
  // Whether that dialog holds unsaved picks (it reports them; see isDirty).
  const [metadataDirty, setMetadataDirty] = useState(false)

  const sortedSteps = useMemo(() => [...steps].sort((a, b) => (a.order - b.order) || (a.id - b.id)), [steps])
  const selected = useMemo(() => sortedSteps.find((s) => s.id === selectedStepId) || null, [sortedSteps, selectedStepId])
  const editingStep = useMemo(() => sortedSteps.find((s) => s.id === editingStepId) || null, [sortedSteps, editingStepId])

  // Unsaved-changes guard. Dirty when an open step editor diverges from the
  // saved baseline, the inline title editor diverges from the saved title, or
  // the metadata dialog holds unsaved picks.
  const isDirty = useMemo(() => {
    if (
      editState
      && editBaseline !== null
      && JSON.stringify(editState) !== editBaseline
    ) return true
    if (editingTitle !== null && editingTitle !== (tour?.title ?? '')) return true
    if (metadataDirty) return true
    return false
  }, [editState, editBaseline, editingTitle, tour, metadataDirty])

  // Mirror into a ref so the (mount-once) beforeunload listener always sees
  // the live value without re-subscribing on every keystroke.
  const isDirtyRef = useRef(false)
  useEffect(() => { isDirtyRef.current = isDirty }, [isDirty])

  // Warn on tab close / refresh / navigation away from the SPA while there are
  // unsaved edits. Modern browsers show their own generic prompt and ignore
  // any custom string, so opting in via preventDefault is all that is needed.
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (!isDirtyRef.current) return
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [])
  // Browser Back / Forward (BrowserRouter has no useBlocker) asks first too.
  // In-app exits go through leaveEditor so they don't stack on its history
  // sentinel.
  const leaveEditor = useUnsavedNavigationGuard(isDirty, t('adminTour.discardChangesConfirm'))
  // Slide above sidebar on a narrow screen instead of a 560px sidebar that
  // leaves the slide no room.
  const stacked = useMediaQuery(TOUR_EDITOR_STACK_QUERY)
  const appHeight = useViewportHeight()
  const layout = tourEditorLayout(stacked, appHeight)

  // Shared confirm for in-app actions that would discard unsaved edits
  // (clicking the dashboard link, switching to edit another step). Returns
  // true when it's safe to proceed.
  function confirmDiscardIfDirty(): boolean {
    return !isDirty || confirm(t('adminTour.discardChangesConfirm'))
  }

  const viewer = useTourViewer({
    tour,
    slide,
    debugMode,
    selectedStep: selected,
    editState,
    editingStep,
    onAddPin: (x, y) => {
      setEditState((prev) => prev ? {
        ...prev,
        pins: [...prev.pins, {
          id: makePinId(),
          x, y,
          shape: pendingPinShape ?? DEFAULT_PIN_SHAPE, color: 'red', rotation_deg: 0, label: '',
          // New pins land on top of every existing annotation.
          z: nextPinZ(prev.pins, prev.shapes_l0),
        }],
      } : prev)
      setPendingPinShape(null)
    },
    onAddShape: (shape) => {
      setEditState((prev) => prev ? ({
        ...prev,
        shapes_l0: [...prev.shapes_l0, normalizeShape(shape)],
      }) : prev)
    },
  })

  async function refresh(signal?: AbortSignal) {
    const d = await adminGetTour(tid, { signal })
    setTour(d.tour)
    setSteps(d.steps || [])
    // The admin slide endpoint serves the parent even while it is a draft
    // (e.g. cascade-unpublished by the slide toggle), so such a tour stays
    // editable.
    const s = await adminGetSlide(d.tour.slide_id, { signal })
    setSlide(s)
    const a = await adminListTourAssets(tid, { signal })
    setAssets(a.items || [])
    setAssetMaxBytes(a.max_bytes ?? null)
  }

  // Adopt the tour revision a write just produced, so this editor's next tour
  // write expects it whether or not a reload follows (or succeeds). Only for
  // a write this editor has already applied to its own state: a step add is
  // not one (its new step arrives with the reload; see addStepFromCurrentView).
  function adoptTourRevision(revision: number) {
    setTour((prev) => (prev ? { ...prev, revision } : prev))
  }

  // Reload after a write that already succeeded. A failure is returned as a
  // message for "saved, but reloading failed" — never reported as the write
  // failing (which would invite a needless retry) nor left as an unhandled
  // rejection.
  async function reloadAfterWrite(): Promise<string | null> {
    try {
      await refresh()
      return null
    } catch (err) {
      return apiErrorMessage(err, t) || t('adminTour.loadFailed')
    }
  }

  // Adopt a tour envelope the server sent with a 409 — its current tour and
  // steps, revisions included — without refresh()'s extra round trips (a
  // tour write never changes the slide or the assets).
  function applyTourDetail(d: TourDetail) {
    setTour(d.tour)
    setSteps(d.steps || [])
  }

  // Shared catch for the tour-structure operations (add / duplicate / move /
  // delete): when the tour changed elsewhere since this editor loaded it, the
  // action computed from the stale copy was refused as a whole, so adopt the
  // server's copy and ask for a retry. Returns false for any other error.
  function handleTourConflict(err: unknown): boolean {
    const current = tourConflictOf(err)
    if (!current) return false
    applyTourDetail(current)
    setMsg(t('adminTour.apiErr.tourConflict'))
    return true
  }

  useEffect(() => {
    // A non-numeric :tourId (typo, hand-edited URL) makes Number() return NaN.
    // Without this guard the editor would fire adminGetTour(NaN) — a guaranteed 422/404 —
    // and strand the admin on an empty editor with an error banner. Bounce
    // back to the dashboard instead.
    if (!Number.isFinite(tid)) {
      nav('/admin/dashboard', { replace: true })
      return
    }
    const ctrl = new AbortController()
    refresh(ctrl.signal).catch((err) => {
      if (isAbortError(err)) return
      // Only bounce back to login on auth failure (missing / revoked staff
      // token). Other failures (network blip, missing tour, 404 on a deleted
      // slide) shouldn't dump the admin onto the login page; surface them in
      // the status banner instead so the admin can react. The login screen
      // replaces this entry, as in AdminDashboard.
      if (isUnauthorizedError(err)) {
        nav('/admin', { replace: true })
        return
      }
      setMsg(apiErrorMessage(err, t) || t('adminTour.loadFailed'))
    })
    return () => ctrl.abort()
  }, [tid])

  // Seed the image adjustment from the slide's saved preset once the slide
  // loads. Keyed on slide identity (not the object ref) so the post-save
  // refresh()—which replaces `slide` with a fresh object every save—doesn't
  // discard the manager's in-session tweaks.
  useEffect(() => {
    if (slide) setImageAdjust(imageAdjustFromSlide(slide))
  }, [slide?.id])

  // Tear down the step editor and every canvas drawing mode in one place so
  // save / cancel / delete can't leave the viewer armed for pin or shape
  // placement (which would otherwise bleed into the next step edited).
  function resetEditorState() {
    setEditingStepId(null)
    setEditState(null)
    setEditBaseline(null)
    setEditRevision(null)
    setStepConflict(null)
    viewer.setAddingPin(false)
    viewer.setAddingShape(null)
    viewer.cancelDraft()
    viewer.setPendingPin(null)
    setPendingPinShape(null)
  }

  // Stored (reference-unit) FOV -> the physical-µm strings the editor's FOV
  // fields hold (see lib/tourStepView). Square-pixel slides convert by
  // exactly 1, so their stored value is shown verbatim; an
  // anisotropic conversion is rounded to 0.01 µm like a captured view so the
  // inputs don't fill with float noise.
  function fovEditFields(rotationDeg: number, w: number, h: number): { fov_um_w: string; fov_um_h: string } {
    const p = fovToPhysical(slide ?? {}, rotationDeg, { w, h })
    const fmt = (physical: number, stored: number) => String(physical === stored ? stored : Number(physical.toFixed(2)))
    return { fov_um_w: fmt(p.w, w), fov_um_h: fmt(p.h, h) }
  }

  function applyCurrentViewToEdit() {
    if (!editState || !slide) return
    const view = viewer.captureCurrentView()
    if (!view) return
    // Rotation is rounded first and the FOV converted at that rounded value,
    // the one saveEdit will convert back with.
    const rot = Number(view.rot.toFixed(2))
    const fov = fovToPhysical(slide, rot, { w: view.fovUmW, h: view.fovUmH })
    const next = {
      rotation_deg: String(rot),
      fov_um_w: String(Number(fov.w.toFixed(2))),
      fov_um_h: String(Number(fov.h.toFixed(2))),
      center_x: String(Number(view.centerX.toFixed(2))),
      center_y: String(Number(view.centerY.toFixed(2))),
    }
    viewer.rememberAppliedView(next)
    setEditState({ ...editState, ...next })
  }

  async function addStepAtPosition(
    basePayload: any,
    afterStepId: number | null,
  ): Promise<{ id: number; revision: number; reorderError: string | null }> {
    // No "promote the former tail" repair here: saveEdit doesn't clamp the
    // tail's transition_mode to 'none', so a step that gains a successor keeps
    // whatever mode the author actually set (or the 'smooth' default for a
    // brand-new step) and animates into the new step without any fix-up.
    // Both requests expect a tour revision: the add the one this editor
    // loaded (a refused add throws the tour conflict to the caller), the
    // reorder the one the add itself produced — this tab's own add must not
    // count as a change made elsewhere.
    const created = await adminAddStep(tid, basePayload, tour?.revision)
    let revision = created.revision
    let reorderError: string | null = null
    if (afterStepId) {
      const ids = sortedSteps.map((st) => st.id)
      const insertAfterIdx = ids.findIndex((id) => id === afterStepId)
      // Skip the reorder when the new step is already at the tail: the server
      // appends with order = max+1, so no extra round-trip is needed.
      if (insertAfterIdx >= 0 && insertAfterIdx + 1 < ids.length) {
        ids.splice(insertAfterIdx + 1, 0, created.id)
        try {
          revision = (await adminReorderSteps(tid, ids, created.revision)).revision
        } catch (err) {
          // The step was created successfully; only the position-adjust
          // failed (e.g. the tour changed elsewhere in between). Surface this
          // to the caller so the UI can warn instead of claiming the whole
          // add failed (and leaving the user wondering where the step
          // actually landed); the caller's reload shows where it is.
          reorderError = apiErrorMessage(err, t) || 'reorder failed'
        }
      }
    }
    return { id: created.id, revision, reorderError }
  }

  // Append-order calculation. Use reduce instead of Math.max(...spread) so
  // a (very) large `steps` array can't trip a stack-size RangeError.
  function nextAppendOrder(): number {
    return steps.reduce((m, s) => Math.max(m, s.order), 0) + 1
  }

  async function addStepFromCurrentView() {
    if (!tour || !slide || tourWriteBusy) return
    setMsg('')
    const view = viewer.captureCurrentView()
    if (!view) return
    setStepBusy(true)

    const label = ''
    const description = ''
    const order = nextAppendOrder()

    // What the request is built from: a pin placed or a step picked while
    // it is out is not undone by its answer.
    const sentPin = viewer.pendingPin
    const selectedAtClick = selectedStepId
    const initialPins: PinItem[] = viewer.pendingPin ? [{
      id: makePinId(),
      x: viewer.pendingPin.x, y: viewer.pendingPin.y,
      shape: DEFAULT_PIN_SHAPE, color: 'red', rotation_deg: 0, label: '',
      z: LAYER_PIN_BASE,
    }] : []
    const payload: any = {
      order,
      label,
      center_x: view.centerX,
      center_y: view.centerY,
      fov_um_w: view.fovUmW,
      fov_um_h: view.fovUmH,
      rotation_deg: view.rot,
      pins: JSON.stringify(initialPins),
      description_md: description,
      transition_mode: DEFAULT_TRANSITION_MODE,
    }

    try {
      const result = await addStepAtPosition(payload, selectedStepId)
      viewer.setPendingPin((p) => (p === sentPin ? null : p))
      // The new step reaches `steps` only through the reload, so the tour
      // revision the add produced is taken from that reload too, never
      // adopted on its own: after a failed reload the editor keeps expecting
      // the revision of the list it still shows, and its next add / move /
      // delete is refused as a tour conflict (which resyncs it) instead of
      // being computed from a list without the new step.
      const reloadError = await reloadAfterWrite()
      setSelectedStepId((cur) => (cur === selectedAtClick ? result.id : cur))
      if (result.reorderError) {
        setMsg(t('adminTour.stepInsertedReorderFailed', { error: result.reorderError }))
      } else if (reloadError) {
        setMsg(t('adminTour.reloadAfterSaveFailed', { error: reloadError }))
      } else {
        setMsg(selectedStepId ? t('adminTour.stepInserted') : t('adminTour.stepAdded'))
      }
    } catch (err) {
      if (!handleTourConflict(err)) setMsg(apiErrorMessage(err, t) || t('adminTour.stepAddFailed'))
    } finally {
      setStepBusy(false)
    }
  }

  async function duplicateStep(step: Step) {
    if (tourWriteBusy) return
    setMsg('')
    setStepBusy(true)
    const selectedAtClick = selectedStepId
    const order = nextAppendOrder()
    const clonedPins: PinItem[] = stepPins(step).map((p) => ({
      id: makePinId(),
      x: p.x, y: p.y,
      shape: normalizePinShape(p.shape),
      color: p.color || 'red',
      rotation_deg: normalizeRotationDeg(p.rotation_deg),
      label: p.label || '',
      z: p.z,
    }))
    const clonedShapes: Shape[] = (step.shapes_l0 || []).map((s) => ({
      ...normalizeShape(s),
      id: makeShapeId(),
    }))
    const dupLabel = step.label ? `${step.label} (copy)` : ''
    const payload: any = {
      order,
      label: dupLabel,
      center_x: step.center_x,
      center_y: step.center_y,
      fov_um_w: step.fov_um_w,
      fov_um_h: step.fov_um_h,
      rotation_deg: step.rotation_deg,
      pins: JSON.stringify(clonedPins),
      // admin_add_step accepts shapes_l0 directly, so no create-then-update
      // round-trip is needed (which would leave a half-cloned step on disk
      // if the follow-up PATCH failed).
      shapes_l0: JSON.stringify(clonedShapes),
      description_md: step.description_md || '',
      transition_mode: normalizeTransitionMode(step.transition_mode),
    }

    try {
      const result = await addStepAtPosition(payload, step.id)
      // The revision comes with the reload, as in addStepFromCurrentView.
      const reloadError = await reloadAfterWrite()
      setSelectedStepId((cur) => (cur === selectedAtClick ? result.id : cur))
      if (result.reorderError) {
        setMsg(t('adminTour.stepDuplicatedReorderFailed', { error: result.reorderError }))
      } else if (reloadError) {
        setMsg(t('adminTour.reloadAfterSaveFailed', { error: reloadError }))
      } else {
        setMsg(t('adminTour.stepDuplicated'))
      }
    } catch (err) {
      if (!handleTourConflict(err)) setMsg(apiErrorMessage(err, t) || t('adminTour.stepDuplicateFailed'))
    } finally {
      setStepBusy(false)
    }
  }

  function startEdit(step: Step) {
    // The FOV fields are converted with the slide's MPP, so the slide must be
    // loaded (refresh() sets the steps a moment before it); and a save in
    // flight must land before the editor is retargeted (see saveEdit). The
    // Edit buttons are disabled in both cases; this is the backstop.
    if (!slide || savingStepId !== null) return
    // Switching into another step's editor would silently replace the current
    // editState; warn first if the open editor has unsaved changes. Re-opening
    // the same step is a no-op refresh, so skip the prompt for it.
    if (editingStepId !== step.id && !confirmDiscardIfDirty()) return
    setSelectedStepId(step.id)
    openEditor(step)
    setMsg('')
  }

  // (Re)initialise the step editor from `step`, which it then treats as
  // saved: the baseline for the unsaved-changes guard and the revision its
  // next save expects. Also the "Reload (discard my changes)" path after a
  // conflict, with the server's copy. Disarms every canvas drawing mode.
  function openEditor(step: Step) {
    viewer.resetLastAppliedView()
    setEditingStepId(step.id)
    const initial: EditState = {
      label: step.label || '',
      description_md: step.description_md || '',
      rotation_deg: String(step.rotation_deg ?? 0),
      ...fovEditFields(step.rotation_deg ?? 0, step.fov_um_w ?? 0, step.fov_um_h ?? 0),
      center_x: String(step.center_x ?? 0),
      center_y: String(step.center_y ?? 0),
      pins: stepPins(step).map((p) => ({
        id: p.id || makePinId(),
        x: p.x, y: p.y,
        shape: normalizePinShape(p.shape),
        color: p.color || 'red',
        rotation_deg: normalizeRotationDeg(p.rotation_deg),
        label: p.label || '',
        z: p.z,
      })),
      shapes_l0: (step.shapes_l0 || []).map(normalizeShape),
      transition_mode: normalizeTransitionMode(step.transition_mode),
    }
    setEditState(initial)
    setEditBaseline(JSON.stringify(initial))
    setEditRevision(step.revision ?? null)
    setStepConflict(null)
    viewer.setPendingPin(null)
    viewer.setAddingPin(false)
    viewer.setAddingShape(null)
    viewer.cancelDraft()
    setPendingPinShape(null)
  }

  // `expectedRevision` defaults to the revision the editor holds; "Overwrite
  // with mine" passes the server's current one from the conflict instead.
  async function saveEdit(stepId: number, expectedRevision: number | null = editRevision) {
    if (!editState || savingStepId !== null) return
    setMsg('')
    // A step needs a label to be saved from here (the server refuses a blank
    // one too). Steps created or restored without one stay as they are until
    // edited; this is where they have to gain one.
    if (editState.label.trim() === '') {
      setMsg(t('adminTour.labelRequired'))
      return
    }
    // Pre-validate numeric viewport fields client-side so a cleared input
    // doesn't snap to 0 (which on level-0 pixels teleports the step to the
    // slide origin) or hit the server only to come back as a generic 422.
    // center_x / center_y are Optional on StepUpdateIn: omitting them
    // leaves the existing DB value untouched. fov_um_w / fov_um_h are
    // required and must be > 0, so a missing or non-finite value is a
    // hard failure surfaced before the network round-trip.
    //
    // Guard the empty string explicitly: Number('') === 0 (which is finite),
    // so a clipped Number.isFinite() check alone would send center_x = 0 for
    // a cleared field and teleport the step to the slide origin. Treat a
    // blank center field as "omit" so the existing DB value is preserved.
    const cxStr = editState.center_x.trim()
    const cyStr = editState.center_y.trim()
    const cx = Number(cxStr)
    const cy = Number(cyStr)
    const rot = Number(editState.rotation_deg.trim())
    const fovW = Number(editState.fov_um_w.trim())
    const fovH = Number(editState.fov_um_h.trim())
    if (!Number.isFinite(rot)) {
      setMsg(t('adminTour.invalidRotation'))
      return
    }
    if (!Number.isFinite(fovW) || fovW <= 0 || !Number.isFinite(fovH) || fovH <= 0) {
      setMsg(t('adminTour.invalidFov'))
      return
    }
    // The server refuses any shape over MAX_FREEHAND_POINTS, and one refused
    // shape fails the whole save. Drawing and normalizeShape already cap
    // strokes, so this only trips if a shape reached the editor some other
    // way — name it (by its card number) rather than surface a bare 422.
    const overCap = findOverCapShape(editState.shapes_l0)
    if (overCap >= 0) {
      setMsg(t('adminTour.shapeTooManyPoints', {
        index: overCap + 1,
        count: editState.shapes_l0[overCap].points.length,
        max: MAX_FREEHAND_POINTS,
      }))
      return
    }
    // The fields hold physical µm; persist the stored units (identity for
    // square pixels; see lib/tourStepView).
    const fov = fovFromPhysical(slide ?? {}, rot, { w: fovW, h: fovH })
    // Persist the author's chosen transition_mode verbatim, even for the tail
    // step whose selector is greyed out. The mode governs how the viewer moves
    // OUT of this step; forward playback never reads the tail's value, but
    // clamping it to 'none' here would be destructive: it would overwrite a
    // real choice that resurfaces the moment the step gains a successor
    // (append) or is reordered out of the tail, producing a hard cut the
    // author never picked.
    // Keeping the value means no "promote the former tail" repair is needed and
    // a deliberate 'none' is never silently rewritten.
    const payload: Record<string, any> = {
      label: editState.label,
      description_md: editState.description_md,
      rotation_deg: rot,
      fov_um_w: fov.w,
      fov_um_h: fov.h,
      pins: editState.pins,
      shapes_l0: editState.shapes_l0,
      transition_mode: editState.transition_mode,
    }
    if (cxStr !== '' && Number.isFinite(cx)) payload.center_x = cx
    if (cyStr !== '' && Number.isFinite(cy)) payload.center_y = cy

    // The exact editor state this request persists. The fields stay editable
    // while it is in flight, so the response must not blindly close the
    // editor: that would discard anything typed (or drawn) after Save was
    // clicked, under a success message.
    const submitted = JSON.stringify(editState)
    setSavingStepId(stepId)
    let result: StepSaveResult
    try {
      const saved = await adminUpdateStep(tid, stepId, payload, expectedRevision)
      // Fold the saved copy in first, so the list is right even if the
      // reload below fails.
      setSteps((prev) => mergeStep(prev, saved.step))
      // The update is persisted from here on, so a failed reload is reported
      // as such — not as a failed save, which would invite a needless retry.
      const reloadError = await reloadAfterWrite()
      result = { kind: 'saved', revision: saved.revision, reloadError }
    } catch (err) {
      const server = stepConflictOf(err)
      if (!server) {
        setSavingStepId(null)
        setMsg(apiErrorMessage(err, t) || t('adminTour.stepUpdateFailed'))
        return
      }
      // Changed elsewhere since this editor loaded it: nothing was saved.
      // Show the server's copy in the list; the editor keeps the user's edits.
      setSteps((prev) => mergeStep(prev, server))
      result = { kind: 'conflict', server }
    }
    setSavingStepId(null)

    // What the editor does next depends on whether it still shows what was
    // sent (refs: this continuation only captured the state at Save time).
    const live = {
      stepId: editingStepIdRef.current,
      state: editStateRef.current ? JSON.stringify(editStateRef.current) : null,
    }
    const outcome = resolveStepSave(stepId, submitted, live, result)
    if (outcome.close) {
      resetEditorState()
    } else if (live.stepId === stepId) {
      if (outcome.baseline !== null) setEditBaseline(outcome.baseline)
      if (outcome.revision !== null) setEditRevision(outcome.revision)
      setStepConflict(outcome.conflict)
    }
    setMsg(t(outcome.message.key, outcome.message.params))
  }

  // "Reload (discard my changes)": restart the editor from the server's copy
  // (the 409 payload, or a newer one a refresh has fetched since), so no
  // extra round trip is needed.
  function reloadConflictedStep() {
    if (!stepConflict) return
    const local = steps.find((s) => s.id === stepConflict.id)
    const latest = local && (local.revision ?? -1) > (stepConflict.revision ?? -1) ? local : stepConflict
    setSteps((prev) => mergeStep(prev, latest))
    openEditor(latest)
    setMsg(t('adminTour.stepReloaded'))
  }

  // "Overwrite with mine": resend the editor's current state (edits made
  // since the refused save included) against the server's current revision.
  function overwriteConflictedStep(stepId: number) {
    if (!stepConflict) return
    void saveEdit(stepId, stepConflict.revision ?? null)
  }

  function nextStepAfter(stepId: number | null): Step | null {
    if (stepId == null) return null
    const idx = sortedSteps.findIndex((st) => st.id === stepId)
    if (idx < 0 || idx + 1 >= sortedSteps.length) return null
    return sortedSteps[idx + 1]
  }

  // "Copy to next step". The server appends the item inside its own write
  // transaction (and mints a fresh id for the empty one sent here), so copies
  // racing on one target — clicked in quick succession here, or made from
  // another window — all land, and nothing else on the target is touched.
  // A client-side read-modify-write of the whole list would need a per-target
  // queue and a re-read before every copy and still lose items to other
  // tabs.
  async function copyAnnotationToNextStep(item: { pin: PinItem } | { shape: Shape }) {
    // The target is fixed at click time: "the next step" as the author saw it.
    const next = nextStepAfter(editingStepId)
    if (!next) return
    setMsg('')
    try {
      const res = 'pin' in item
        ? await adminAppendStepPin(tid, next.id, { ...item.pin, id: '' })
        : await adminAppendStepShape(tid, next.id, normalizeShape({ ...item.shape, id: '' }))
      // The response carries the updated target; mergeStep keeps the newest
      // revision, so responses arriving out of order can't drop a copy from
      // the list either. No reload needed.
      setSteps((prev) => mergeStep(prev, res.step))
    } catch (err) {
      // Coded errors (the per-step annotation cap) say what went wrong.
      setMsg(isApiCodeError(err) ? apiErrorMessage(err, t) : t('adminTour.copyToNextFailed'))
      return
    }
    setMsg(t('pin' in item ? 'adminTour.pinCopiedToNext' : 'adminTour.shapeCopiedToNext'))
  }

  async function moveStep(stepId: number, direction: -1 | 1) {
    // The new order is derived from the current `steps` snapshot, so a second
    // click before the first reorder lands would compute from stale order and
    // clobber it. Share the create-step guard: add / duplicate also compute
    // positions from the same snapshot.
    if (tourWriteBusy) return
    const idx = sortedSteps.findIndex((st) => st.id === stepId)
    const target = idx + direction
    if (idx < 0 || target < 0 || target >= sortedSteps.length) return
    const ids = sortedSteps.map((st) => st.id)
    ;[ids[idx], ids[target]] = [ids[target], ids[idx]]
    setMsg('')
    setStepBusy(true)
    try {
      // Expects the tour revision the order above was computed from, so on
      // success the server holds exactly that order: apply it here instead of
      // reloading the tour.
      const res = await adminReorderSteps(tid, ids, tour?.revision)
      setSteps((prev) => applyStepOrder(prev, ids))
      adoptTourRevision(res.revision)
      setMsg(t('adminTour.stepOrderUpdated'))
    } catch (err) {
      if (!handleTourConflict(err)) setMsg(apiErrorMessage(err, t) || t('adminTour.stepReorderFailed'))
    } finally {
      setStepBusy(false)
    }
  }

  async function delStep(stepId: number) {
    if (tourWriteBusy || !confirm(t('adminTour.deleteStepConfirm'))) return
    setMsg('')
    let revision: number
    setStepBusy(true)
    try {
      // Expects the tour revision of the step list the user confirmed from.
      revision = (await adminDeleteStep(tid, stepId, tour?.revision)).revision
    } catch (err) {
      if (!handleTourConflict(err)) setMsg(apiErrorMessage(err, t) || t('adminTour.stepDeleteFailed'))
      return
    } finally {
      setStepBusy(false)
    }
    // As things stand now, not at the click: a step picked or opened for
    // editing while the delete was out is left alone.
    setSelectedStepId((cur) => (cur === stepId ? null : cur))
    // If the deleted step is being edited, drop the editor state so its
    // stale pins/shapes don't keep rendering as ghost overlays on top of
    // whichever step ends up selected next.
    if (editingStepIdRef.current === stepId) resetEditorState()
    // The server renumbered the remaining steps 1..n in their order; the
    // expected revision guarantees that is this list minus the step.
    setSteps((prev) => applyStepOrder(prev, sortedSteps.map((st) => st.id).filter((id) => id !== stepId)))
    adoptTourRevision(revision)
  }

  async function changeVisibility(target: Visibility) {
    if (!tour || tourWriteBusy) return
    const current = visibilityOf(tour)
    if (target === current) return
    setMsg('')
    setVisBusy(true)
    // The calls expect the revision this editor loaded, threaded from one to
    // the next (see tourVisibilityApi); `vis.revision()` is the latest.
    const vis = tourVisibilityApi(tid, tour.revision)
    // Adopt the server-confirmed flags and the revision the calls produced.
    const adopt = (flags: { is_published: boolean; unlisted: boolean }) =>
      setTour((prev) => (prev ? { ...prev, ...flags, revision: vis.revision() ?? prev.revision } : prev))
    try {
      const r = await applyVisibility(
        { is_published: tour.is_published, unlisted: tour.unlisted },
        target,
        vis.api,
      )
      adopt({ is_published: r.is_published, unlisted: r.unlisted })
    } catch (err) {
      if (err instanceof VisibilityError) {
        adopt({ is_published: err.partial.is_published, unlisted: err.partial.unlisted })
      }
      const cause = err instanceof VisibilityError ? err.cause : err
      const current = tourConflictOf(cause)
      if (current) {
        // The flags this choice was computed from were stale (changed in
        // another window, or by the slide's unpublish cascade); show the
        // server's state to choose from again.
        applyTourDetail(current)
        setMsg(t('adminTour.apiErr.tourConflict'))
        return
      }
      // Server returns 409 when trying to publish a tour while its parent
      // slide is still a draft. Surface the actionable message instead of the
      // raw error so the admin knows to publish the slide first.
      if (/parent slide is a draft/i.test((cause as Error)?.message || '')) {
        setMsg(t('adminTour.publishRequiresSlidePublished'))
      } else {
        setMsg(apiErrorMessage(cause, t) || t('adminTour.publishFailed'))
      }
      // Resync from server truth in case a call's outcome is unknown (a
      // dropped connection); best effort — the message above already says
      // what failed.
      await reloadAfterWrite()
    } finally {
      setVisBusy(false)
    }
  }

  async function saveTitle() {
    if (editingTitle == null || titleSavingRef.current || tourWriteBusy) return
    const title = editingTitle.trim()
    if (!title) {
      setMsg(t('adminTour.titleRequired'))
      return
    }
    setMsg('')
    titleSavingRef.current = true
    setTitleSaving(true)
    try {
      // The response carries the whole saved tour (new revision included);
      // a title change touches nothing else, so no reload.
      const res = await adminUpdateTour(tid, { title }, tour?.revision)
      setTour(res.tour)
      setEditingTitle(null)
      setTitleConflict(null)
    } catch (err) {
      const current = tourConflictOf(err)
      if (current) {
        // The tour changed elsewhere since this editor loaded it. Keep the
        // typed title open, adopt the server's tour (so an explicit retry
        // expects its current revision) and show what the title is now; the
        // banner offers to drop the edit or save it over theirs.
        applyTourDetail(current)
        setTitleConflict(current.tour.title)
        return
      }
      setMsg(apiErrorMessage(err, t) || t('adminTour.titleUpdateFailed'))
    } finally {
      titleSavingRef.current = false
      setTitleSaving(false)
    }
  }

  function closeTitleEditor() {
    // Closing mid-save would not stop it: the title still changes.
    if (titleSavingRef.current) return
    setEditingTitle(null)
    setTitleConflict(null)
    setMsg('')
  }

  async function deleteTour() {
    if (tourWriteBusy || savingStepId !== null) return
    // Surface impact: deleting a tour cascades to all steps and removes the
    // on-disk asset directory (admin_tours.py:admin_delete_tour). Show those
    // counts in the confirm so a slip on the delete button can't silently
    // wipe out a tour with dozens of edited steps or uploaded assets.
    if (!confirm(t('adminTour.deleteTourConfirm', {
      title: tour?.title || '',
      stepCount: steps.length,
      assetCount: assets.length,
    }))) return
    await deleteTourRequest.run(async () => {
      try {
        await adminDeleteTour(tid)
        // Replace this page's entry: Back must not reopen the deleted tour.
        leaveEditor(() => nav('/admin/dashboard', { replace: true }))
      } catch (err: any) {
        setMsg(apiErrorMessage(err, t) || t('adminTour.deleteTourFailed'))
      }
    })
  }

  function insertMarkdownToEditor(md: string): boolean {
    if (!editState) return false
    const curr = editState.description_md || ''
    const joiner = curr && !curr.endsWith('\n') ? '\n\n' : ''
    setEditState({ ...editState, description_md: `${curr}${joiner}${md}` })
    return true
  }

  return (
    <div style={layout.container}>
      <div style={layout.main}>
        {navOpen ? (
        <div style={{ position: 'absolute', top: 60, left: 10, zIndex: 10, background: 'rgba(255,255,255,0.92)', padding: '6px 30px 6px 10px', borderRadius: 10, display: 'flex', flexDirection: 'column', gap: 6, maxWidth: 'calc(100% - 20px)' }}>
          <button
            type="button"
            onClick={() => setNavOpen(false)}
            title={t('adminTour.collapseNav')}
            aria-label={t('adminTour.collapseNav')}
            aria-expanded
            style={{ position: 'absolute', top: 4, right: 4, width: 24, height: 24, display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--color-surface-muted)', border: '1px solid var(--color-border-strong)', cursor: 'pointer', color: 'var(--color-text)', fontSize: 16, fontWeight: 700, lineHeight: 1, padding: 0, borderRadius: '50%', boxShadow: 'var(--shadow-sm)' }}
          >
            −
          </button>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <Link
              to="/admin/dashboard"
              onClick={(e) => {
                // A modified click opens a new tab and leaves the editor as is.
                if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
                e.preventDefault()
                if (confirmDiscardIfDirty()) leaveEditor(() => nav('/admin/dashboard'))
              }}
            >
              {t('admin.dashboard')}
            </Link>
            {editingTitle === null ? (
              <>
                <span style={{ fontWeight: 800 }}>{tour?.title || ''}</span>
                <button type="button" onClick={() => setEditingTitle(tour?.title || '')} disabled={!tour}>{t('adminTour.editTitle')}</button>
              </>
            ) : (
              <>
                <input
                  value={editingTitle}
                  autoFocus
                  readOnly={tourWriteBusy}
                  aria-busy={titleSaving}
                  maxLength={255}
                  onChange={(e) => setEditingTitle(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') { e.preventDefault(); saveTitle() }
                    else if (e.key === 'Escape') { e.preventDefault(); closeTitleEditor() }
                  }}
                  style={{ fontWeight: 700, minWidth: 200 }}
                />
                <button type="button" onClick={saveTitle} disabled={tourWriteBusy}>{t('adminTour.saveTitle')}</button>
                <button type="button" onClick={closeTitleEditor} disabled={titleSaving}>{t('adminTour.cancel')}</button>
              </>
            )}
          </div>
          {editingTitle !== null && titleConflict !== null && (
            <ConflictBanner
              message={t('adminTour.titleConflict', { title: titleConflict })}
              reloadLabel={t('adminTour.conflictReload')}
              overwriteLabel={t('adminTour.conflictOverwrite')}
              // The tour was already adopted from the 409, so dropping the
              // edit is just closing the input, and saving again expects the
              // server's current revision.
              onReload={closeTitleEditor}
              onOverwrite={saveTitle}
              disabled={tourWriteBusy}
            />
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <VisibilitySelect
              size="sm"
              ariaLabel={t('adminDash.visLabel')}
              value={tour ? visibilityOf(tour) : 'draft'}
              labels={{
                draft: t('adminDash.visDraft'),
                unlisted: t('adminDash.visLinkOnly'),
                public: t('adminDash.visPublic'),
              }}
              disabled={!tour || tourWriteBusy}
              linkOnlyTitle={t('adminDash.linkOnlyHint')}
              onChange={changeVisibility}
            />
            <button
              type="button"
              onClick={() => setMetadataOpen(true)}
              // Its save sends the tour revision too.
              disabled={!tour || !slide || tourWriteBusy}
              title={t('adminTour.metadataTitle')}
            >
              {(() => {
                const n = Object.values(tour?.attributes || {}).reduce((sum, vals) => sum + vals.length, 0)
                return n > 0 ? `${t('adminTour.metadata')} (${n})` : t('adminTour.metadata')
              })()}
            </button>
            {tour?.slide_id && (
              <LinkButton
                size="sm"
                variant="secondary"
                to={`/slides/${tour.slide_public_id}/tours/${tour.public_id}?from=admin`}
                {...adminPreviewLinkProps}
                title={tour.is_published ? undefined : t('adminDash.openViewerDraftHint')}
              >
                {tour.is_published ? t('adminTour.previewTour') : t('adminTour.previewTourDraft')}
              </LinkButton>
            )}
            <button onClick={deleteTour} disabled={!tour || tourWriteBusy || savingStepId !== null}>{t('adminTour.deleteTour')}</button>
            {tour?.is_published && tour?.slide_public_id && tour?.public_id && (() => {
              // 1-based position of the selected step within the ordered list,
              // or 0 when no step is selected (→ plain tour URL). Step 1 also
              // yields the plain tour URL (see TourPublicUrl).
              const selectedStepNumber = selectedStepId
                ? sortedSteps.findIndex((s) => s.id === selectedStepId) + 1
                : 0
              return (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <span style={{ fontSize: 12, color: 'var(--color-text-muted)' }}>
                    {selectedStepNumber > 1
                      ? t('adminTour.stepLinkLabel', { step: selectedStepNumber })
                      : t('adminTour.tourLinkLabel')}
                  </span>
                  <TourPublicUrl
                    slidePublicId={tour.slide_public_id}
                    tourPublicId={tour.public_id}
                    stepNumber={selectedStepNumber || undefined}
                  />
                </div>
              )
            })()}
          </div>
        </div>
        ) : (
          <button
            type="button"
            onClick={() => setNavOpen(true)}
            title={t('adminTour.expandNav')}
            aria-label={t('adminTour.expandNav')}
            aria-expanded={false}
            style={{ position: 'absolute', top: 60, left: 10, zIndex: 10, background: 'rgba(255,255,255,0.82)', border: 'none', borderRadius: 5, cursor: 'pointer', color: 'rgba(0,0,0,0.85)', padding: '4px 8px', lineHeight: 1, display: 'flex', alignItems: 'center', gap: 6, boxShadow: 'var(--shadow-sm)', maxWidth: 'calc(100% - 20px)' }}
          >
            <span aria-hidden style={{ fontSize: 14 }}>☰</span>
            {tour?.title && (
              <span style={{ maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 13, fontWeight: 600 }}>{tour.title}</span>
            )}
          </button>
        )}
        <div
          ref={viewer.viewerDivRef}
          className="osd-with-adjust"
          style={{
            width: '100%',
            height: '100%',
            ['--osd-filter' as any]: cssFilterString(imageAdjust),
          }}
        />
        {viewer.loadError && (
          <div style={{
            position: 'absolute', top: 12, left: 12, right: 12, zIndex: 20,
            background: 'var(--color-danger-bg)', color: 'var(--color-danger-fg)',
            padding: '8px 12px', borderRadius: 6, fontSize: 13,
          }}>
            {t(viewer.loadError)}
          </div>
        )}
        <div ref={viewer.scaleBarDivRef as any}></div>
        <DebugToggle enabled={debugMode} onToggle={toggleDebug} />
        {/* Placing a pin or a shape shows the coordinates too, debug mode or not. */}
        {(debugMode || viewer.addingPin || !!viewer.addingShape) && <MouseCoordsOverlay coords={viewer.mouseCoords} />}
        <ImageAdjustControls
          value={imageAdjust}
          onChange={setImageAdjust}
          baseline={imageAdjustFromSlide(slide)}
          canSavePreset={isManagerOrAboveFromToken()}
          onSavePreset={async (v) => {
            if (!slide) return
            const res = await adminSaveSlideImageAdjust(slide.id, v)
            // Fold the persisted preset back into the in-memory slide so the
            // baseline (reset target / "modified" dot) updates without a reload.
            setSlide((prev) => prev ? {
              ...prev,
              image_adjust_brightness: res.image_adjust_brightness,
              image_adjust_contrast: res.image_adjust_contrast,
              image_adjust_saturation: res.image_adjust_saturation,
            } : prev)
          }}
        />
      </div>

      <aside style={layout.aside}>
        <div style={{ padding: 12, borderBottom: '1px solid var(--color-border)' }}>
          <div style={{ fontSize: 12, opacity: 0.8 }}>{t('adminTour.slideId', { id: tour?.slide_id || '' })}</div>
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <button onClick={addStepFromCurrentView} disabled={tourWriteBusy}>{selectedStepId ? t('adminTour.insertAfterSelected') : t('adminTour.addStepFromView')}</button>
          </div>
          {selected && <div style={{ fontSize: 12, marginTop: 6, opacity: 0.85 }}>{t('adminTour.selectedStep', { order: selected.order, label: selected.label || t('adminTour.untitledItem') })}</div>}
          {viewer.pendingPin && <div style={{ fontSize: 12, marginTop: 6 }}>{t('adminTour.pinSelected', { x: viewer.pendingPin.x.toFixed(0), y: viewer.pendingPin.y.toFixed(0) })}</div>}
          {msg && <div style={{ fontSize: 12, marginTop: 6, opacity: 0.8 }}>{msg}</div>}
          <div style={{ fontSize: 12, marginTop: 8, opacity: 0.7 }}>
            {t('adminTour.tip')}
          </div>
        </div>

        <div className="overscroll-panel" style={{ flex: 1, overflow: 'auto', padding: 12 }}>
          <TourAssetsPanel
            tourId={tid}
            tourPublicId={tour?.public_id ?? ''}
            assets={assets}
            setAssets={setAssets}
            maxBytes={assetMaxBytes}
            onInsertMarkdown={insertMarkdownToEditor}
            editorOpen={editState !== null}
          />

          <h3 style={{ marginTop: 18 }}>{t('adminTour.stepsTitle')}</h3>
          {sortedSteps.map((s, idx) => (
            <div key={s.id} onClick={() => { setSelectedStepId(s.id); viewer.jumpToStep(s) }} style={{ border: selectedStepId === s.id ? '2px solid var(--color-brand-600)' : '1px solid var(--color-border)', borderRadius: 10, padding: 10, marginBottom: 10, cursor: 'pointer' }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <div style={{ fontWeight: 800 }}>{s.order}. {s.label}</div>
                <button style={{ marginLeft: 'auto' }} onClick={(e) => { e.stopPropagation(); moveStep(s.id, -1) }} disabled={tourWriteBusy || idx === 0}>↑</button>
                <button onClick={(e) => { e.stopPropagation(); moveStep(s.id, 1) }} disabled={tourWriteBusy || idx === sortedSteps.length - 1}>↓</button>
                <button onClick={(e) => { e.stopPropagation(); duplicateStep(s) }} disabled={tourWriteBusy}>{t('adminTour.duplicate')}</button>
                {/* The inline editor already carries Save/Cancel; re-showing
                    Edit for the open step only invites a silent re-init that
                    discards in-progress changes. */}
                {editingStepId !== s.id && (
                  <button onClick={(e) => { e.stopPropagation(); startEdit(s) }} disabled={!slide || savingStepId !== null}>{t('adminTour.edit')}</button>
                )}
                <button onClick={(e) => { e.stopPropagation(); delStep(s.id) }} disabled={tourWriteBusy || savingStepId === s.id}>{t('adminTour.delete')}</button>
              </div>

              {editingStepId === s.id && editState ? (
                <TourStepEditForm
                  editState={editState}
                  setEditState={setEditState}
                  hasNext={nextStepAfter(s.id) !== null}
                  addingPin={viewer.addingPin}
                  setAddingPin={viewer.setAddingPin}
                  pendingPinShape={pendingPinShape}
                  setPendingPinShape={setPendingPinShape}
                  addingShape={viewer.addingShape}
                  setAddingShape={viewer.setAddingShape}
                  cancelDraft={viewer.cancelDraft}
                  onApplyCurrentView={applyCurrentViewToEdit}
                  onCopyPinToNext={(pin) => { void copyAnnotationToNextStep({ pin }) }}
                  onCopyShapeToNext={(shape) => { void copyAnnotationToNextStep({ shape }) }}
                  saving={savingStepId === s.id}
                  conflict={stepConflict !== null && stepConflict.id === s.id}
                  onReloadConflict={reloadConflictedStep}
                  onOverwriteConflict={() => overwriteConflictedStep(s.id)}
                  fovEstimated={!!slide && !slidePixelSize(slide)}
                  onSave={() => saveEdit(s.id)}
                  onCancel={() => {
                    // Warn before discarding unsaved changes (isDirty compares
                    // against the snapshot taken at startEdit so brand-new
                    // pins / shapes / text edits don't get tossed silently).
                    if (!confirmDiscardIfDirty()) return
                    resetEditorState()
                  }}
                />
              ) : (
                <>
                  <div style={{ fontSize: 12, opacity: 0.8, marginTop: 6 }}>
                    {(() => {
                      // Physical µm, matching the editor's FOV fields (lib/tourStepView).
                      const fov = fovToPhysical(slide ?? {}, s.rotation_deg, { w: s.fov_um_w, h: s.fov_um_h })
                      return t('adminTour.stepMetrics', { centerX: Math.round(s.center_x), centerY: Math.round(s.center_y), fovW: Math.round(fov.w), fovH: Math.round(fov.h), rot: s.rotation_deg })
                    })()}
                  </div>
                  <div style={{ marginTop: 8 }}>
                    <SafeMarkdown markdown={s.description_md || ''} adminImages />
                  </div>
                </>
              )}
            </div>
          ))}
        </div>

        <div style={{ borderTop: '1px solid var(--color-border)', padding: 10, fontSize: 12, opacity: 0.75 }}>
          {t('adminTour.sanitizedFooter')}
        </div>
      </aside>
      {metadataOpen && tour && slide && (
        <TourMetadataModal
          tour={tour}
          slideAttributes={slide.attributes || {}}
          onSaved={(saved) => { setTour(saved); setMsg(t('adminTour.metadataSaved')) }}
          onConflict={applyTourDetail}
          onClose={() => setMetadataOpen(false)}
          onDirtyChange={setMetadataDirty}
        />
      )}
    </div>
  )
}
