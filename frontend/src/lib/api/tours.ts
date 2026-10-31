import { adminGet, adminSend, bearerHeaders, throwApi, FileTooLargeError, isApiCodeError, type Slide } from './shared'

export type PinShape = 'pin' | 'circle' | 'arrow'
export type ShapeLabelPosition = 'above' | 'below' | 'center'
export type ShapeKind = 'rect' | 'circle' | 'freehand'
export type ShapePoint = { x: number; y: number }
// Tour-step shape annotation in level-0 pixel coordinates. The same type
// carries rectangles, circles, and freehand strokes — discriminated by
// `kind`. For freehand, (x, y, w, h) hold the bounding box (recomputed on
// save) and `points` holds the stroke vertices.
export type Shape = {
  id: string
  kind: ShapeKind
  x: number
  y: number
  w: number
  h: number
  stroke_color: string
  fill_enabled: boolean
  fill_opacity: number
  points: ShapePoint[]
  // Freehand-only: render a smoothed curve (a low-pass of the vertices, so it
  // erases the low-zoom "staircase" rather than just rounding its corners)
  // instead of straight segments. Defaults true. Ignored by rect/circle.
  smoothing: boolean
  label: string
  label_position: ShapeLabelPosition
  // Stacking order shared with PinItem.z (higher = on top). Shapes default to
  // 0 and pins to LAYER_PIN_BASE, so pins sit above shapes unless reordered.
  // See lib/tourLayers.
  z: number
}

export type PinItem = {
  id: string
  x: number
  y: number
  shape: PinShape
  color: string
  rotation_deg: number
  label: string
  // Stacking order shared with Shape.z; see lib/tourLayers.
  z: number
}

export type TourSummary = {
  id: number
  // The tour's own unguessable public identifier, and the parent slide's —
  // both are needed to build the public tour URL and its tile source. The
  // numeric ids stay for internal keying / admin calls.
  public_id: string
  slide_public_id: string
  slide_id: number
  title: string
  description: string
  is_published: boolean
  // Link-only ("unlisted") visibility. When true, a published tour is
  // reachable by direct URL but hidden from the public tour listing and from
  // its parent slide's tour list. Optional so public responses (which already
  // omit unlisted tours) needn't carry it.
  unlisted?: boolean
  // Optimistic-concurrency counter for the tour's own fields and its step
  // structure (add / delete / reorder). Admin responses only (public ones
  // send null). Echoed back as the expected revision on every tour write;
  // see "Tour editing concurrency" in docs/developer-guide.md.
  revision?: number | null
  // The tour's OWN metadata, {key: [values]}, on top of what it inherits from
  // its slide. On admin responses, the public tour (GET /api/tours/{id}) and
  // the Tours view's items; the other public listings omit it.
  attributes?: Record<string, string[]> | null
}

// How a step animates the move OUT of itself into the next step. Mirrors the
// server's allowed `transition_mode` values (schemas.TRANSITION_MODES).
export type TransitionMode = 'none' | 'smooth' | 'smooth_zoom'
const TRANSITION_MODES: readonly TransitionMode[] = ['none', 'smooth', 'smooth_zoom']
// New steps default to a plain smooth pan/zoom to the next framing (no
// zoom-out-then-in flourish), which most viewers find calmer to follow.
export const DEFAULT_TRANSITION_MODE: TransitionMode = 'smooth'

// Coerce an arbitrary server/stored value to a known mode, falling back to the
// default. Used wherever a step's mode is read (viewer + editor).
export function normalizeTransitionMode(v: unknown): TransitionMode {
  return (TRANSITION_MODES as readonly string[]).includes(v as string)
    ? (v as TransitionMode)
    : DEFAULT_TRANSITION_MODE
}

export type TourStep = {
  id: number
  order: number
  label: string
  center_x: number
  center_y: number
  fov_um_w: number
  fov_um_h: number
  rotation_deg: number
  pins: PinItem[]
  shapes_l0: Shape[]
  description_md: string
  transition_mode?: TransitionMode
  // Optimistic-concurrency counter for this step's own content (label,
  // description, view, transition, pins, shapes); the step editor sends the
  // revision it was opened from with every save.
  revision?: number
}

// Shape returned by GET /api/tours/{id} and the admin equivalent.
// Both pages render the same { tour, steps } envelope, so the shared type
// lives here next to the request helpers.
export type TourDetail = {
  tour: TourSummary
  steps: TourStep[]
}

// ===== Public =====

// Every listed tour, ordered by (slide_id, id). The index page's "All" view
// uses it for each slide card's inline tour list; it carries no slide data.
export async function listAllPublishedTours(opts: { signal?: AbortSignal } = {}): Promise<TourSummary[]> {
  const r = await fetch('/api/tours?published_only=1', { signal: opts.signal })
  if (!r.ok) await throwApi(r, 'tours failed')
  return r.json()
}

// The parent-slide fields a tour card renders (thumbnail + caption). A full
// Slide satisfies it, so TourTile / TourRow accept either.
export type TourIndexSlide = Pick<
  Slide,
  'id' | 'public_id' | 'catalog_id' | 'title' | 'thumb_url' | 'attributes'
  | 'image_adjust_brightness' | 'image_adjust_contrast' | 'image_adjust_saturation'
>

export type TourIndexItem = TourSummary & { slide: TourIndexSlide }

export type TourIndexResponse = {
  items: TourIndexItem[]
  // Size of the whole match set; per_page caps `items`.
  total: number
  page: number
  per_page: number
}

// GET /api/index/tours — the index page's "Tours" view, searched (q on the
// tour title or the parent slide's own fields), filtered (the /api/slides
// stain / organ / lab / tags / facet params, applied to the parent), sorted
// (by the tour's own title for sort=title, else in slide-list order) and
// capped server-side.
export async function listTourIndex(params: URLSearchParams, opts: { signal?: AbortSignal } = {}): Promise<TourIndexResponse> {
  const r = await fetch('/api/index/tours?' + params.toString(), { signal: opts.signal })
  if (!r.ok) await throwApi(r, 'tours failed')
  return r.json()
}

// Public detail: keyed by the tour's public_id token (never the numeric id).
// Pass ``adminToken`` to preview a draft tour (endpoint serves drafts to a
// valid admin JWT).
export async function getTour(
  tourPublicId: string,
  opts: { signal?: AbortSignal; adminToken?: string | null } = {},
): Promise<TourDetail> {
  const r = await fetch(`/api/tours/${encodeURIComponent(tourPublicId)}`, {
    signal: opts.signal,
    headers: bearerHeaders(opts.adminToken),
  })
  if (!r.ok) await throwApi(r, 'tour failed')
  return r.json()
}

// ===== Admin: tours =====

export async function adminCreateTour(slideId: number, title: string) {
  const form = new FormData()
  form.append('title', title)
  return adminSend<{ id: number }>('POST', `/api/admin/slides/${slideId}/tours`, form, 'create tour failed')
}

// A draft copy of the tour on the same slide (steps, annotations, own
// metadata and uploaded assets included).
export async function adminDuplicateTour(tourId: number, title: string) {
  const form = new FormData()
  form.append('title', title)
  return adminSend<{ id: number; public_id: string }>('POST', `/api/admin/tours/${tourId}/duplicate`, form, 'duplicate tour failed')
}

export async function adminListAllTours(opts: { signal?: AbortSignal } = {}) {
  return adminGet<TourSummary[]>('/api/admin/tours', 'list all tours failed', { signal: opts.signal })
}

export async function adminGetTour(
  tourId: number,
  opts: { signal?: AbortSignal } = {},
): Promise<TourDetail> {
  return adminGet(`/api/admin/tours/${tourId}`, 'admin get tour failed', { signal: opts.signal })
}

// ===== Optimistic concurrency =====
//
// Every tour/step mutation below takes the revision the caller's copy was
// loaded at (`expectedRevision`) — the tour's for tour fields and for step
// add / delete / reorder, the step's for a step save. The server applies the
// write only if that is still current and otherwise answers 409 with one of
// these codes plus its current copy (read it with stepConflictOf /
// tourConflictOf). null / undefined sends none: the write is unconditional,
// which only scripts should rely on.
export const STEP_CONFLICT_CODE = 'adminTour.apiErr.stepConflict'
export const TOUR_CONFLICT_CODE = 'adminTour.apiErr.tourConflict'

type ExpectedRevision = number | null | undefined

function hasRevision(rev: ExpectedRevision): rev is number {
  return typeof rev === 'number' && Number.isFinite(rev)
}

function withRevision<T extends object>(body: T, rev: ExpectedRevision): T & { expected_revision?: number } {
  return hasRevision(rev) ? { ...body, expected_revision: rev } : body
}

function appendRevision(form: FormData, rev: ExpectedRevision) {
  if (hasRevision(rev)) form.append('expected_revision', String(rev))
}

// The server's current step when `err` is a step-revision conflict, else null.
export function stepConflictOf(err: unknown): TourStep | null {
  if (!isApiCodeError(err) || err.code !== STEP_CONFLICT_CODE) return null
  const cur = err.current as TourStep | undefined
  return cur && typeof cur === 'object' && typeof cur.id === 'number' ? cur : null
}

// The server's current tour envelope ({ tour, steps }) when `err` is a
// tour-revision conflict, else null.
export function tourConflictOf(err: unknown): TourDetail | null {
  if (!isApiCodeError(err) || err.code !== TOUR_CONFLICT_CODE) return null
  const cur = err.current as TourDetail | undefined
  return cur && typeof cur === 'object' && cur.tour && Array.isArray(cur.steps) ? cur : null
}

export async function adminUpdateTour(
  tourId: number,
  // `attributes` replaces the tour's own metadata as a whole ({} clears it).
  payload: { title?: string; description?: string; attributes?: Record<string, string[]> },
  expectedRevision?: ExpectedRevision,
) {
  return adminSend<{ ok: boolean; revision: number; tour: TourSummary }>(
    'PATCH',
    `/api/admin/tours/${tourId}`,
    withRevision(payload, expectedRevision),
    'update tour failed',
  )
}

export async function adminDeleteTour(tourId: number) {
  return adminSend('DELETE', `/api/admin/tours/${tourId}`, undefined, 'delete tour failed')
}

export async function adminPublishTour(tourId: number, publish: boolean, expectedRevision?: ExpectedRevision) {
  const form = new FormData()
  form.append('publish', publish ? '1' : '0')
  appendRevision(form, expectedRevision)
  return adminSend<{ ok: boolean; is_published: boolean; revision: number }>(
    'POST',
    `/api/admin/tours/${tourId}/publish`,
    form,
    'publish failed',
  )
}

// Toggle a tour's link-only ("unlisted") visibility. Independent of publish:
// hides the tour from the public listing while keeping its direct URL usable.
export async function adminSetTourUnlisted(tourId: number, unlisted: boolean, expectedRevision?: ExpectedRevision) {
  const form = new FormData()
  form.append('unlisted', unlisted ? '1' : '0')
  appendRevision(form, expectedRevision)
  return adminSend<{ ok: boolean; unlisted: boolean; revision: number }>(
    'POST',
    `/api/admin/tours/${tourId}/unlisted`,
    form,
    'set unlisted failed',
  )
}

// The publish / unlisted pair for lib/visibility's applyVisibility, threading
// the tour revision through: applyVisibility may make both calls back to
// back, and the second must expect the revision the first produced, not the
// one the caller loaded. `revision()` reads the latest, for the caller's
// local copy afterwards.
export function tourVisibilityApi(tourId: number, revision: ExpectedRevision) {
  let rev: ExpectedRevision = revision
  return {
    api: {
      publish: async (v: boolean) => {
        const r = await adminPublishTour(tourId, v, rev)
        rev = r.revision
        return { is_published: r.is_published }
      },
      setUnlisted: async (v: boolean) => {
        const r = await adminSetTourUnlisted(tourId, v, rev)
        rev = r.revision
        return { unlisted: r.unlisted }
      },
    },
    revision: () => rev,
  }
}

// ===== Admin: bulk tour operations =====
//
// One server-side batch per action, like the slide bulk helpers in admin.ts
// (looping per-tour requests would trip nginx's rate limit). Each tour is
// sent with the revision the dashboard listed it at: a tour changed since is
// skipped with error "conflict" instead of overwritten.

function bulkTourRefs(tours: Pick<TourSummary, 'id' | 'revision'>[]) {
  return tours.map((tour) => withRevision({ id: tour.id }, tour.revision))
}

export type BulkTourResult = {
  tour_id: number
  ok: boolean
  // False for a tour the change left as it was (it keeps its revision).
  changed: boolean
  // not_found | conflict | parent_draft | not_registered | failed
  error: string | null
  revision?: number
  // Visibility results only: the flags the tour now has.
  is_published?: boolean
  unlisted?: boolean
}

export async function adminBulkTourVisibility(
  tours: Pick<TourSummary, 'id' | 'revision'>[],
  visibility: 'draft' | 'unlisted' | 'public',
): Promise<{ ok: boolean; results: BulkTourResult[] }> {
  return adminSend(
    'POST',
    '/api/admin/tours/bulk/visibility',
    { tours: bulkTourRefs(tours), visibility },
    'bulk tour visibility failed',
  )
}

export async function adminBulkTourAttributes(
  tours: Pick<TourSummary, 'id' | 'revision'>[],
  key: string,
  values: string[],
  mode: 'add' | 'remove',
): Promise<{ ok: boolean; results: BulkTourResult[] }> {
  return adminSend(
    'POST',
    '/api/admin/tours/bulk/attributes',
    { tours: bulkTourRefs(tours), key, values, mode },
    'bulk tour attributes failed',
  )
}

export type BulkTourDeleteResult = {
  ok: boolean
  deleted: number[]
  not_found: number[]
  failed: { tour_id: number; error: string }[]
}

export async function adminBulkDeleteTours(tourIds: number[]): Promise<BulkTourDeleteResult> {
  return adminSend('POST', '/api/admin/tours/bulk/delete', { tour_ids: tourIds }, 'bulk tour delete failed')
}

// ===== Admin: steps =====

// `expectedRevision` is the TOUR's: adding a step changes its structure. The
// returned `revision` is the tour's new one (the step starts at 0).
export async function adminAddStep(tourId: number, payload: Record<string, any>, expectedRevision?: ExpectedRevision) {
  const form = new FormData()
  Object.entries(payload).forEach(([k, v]) => form.append(k, String(v)))
  appendRevision(form, expectedRevision)
  return adminSend<{ id: number; revision: number }>(
    'POST',
    `/api/admin/tours/${tourId}/steps`,
    form,
    'add step failed',
  )
}

// `expectedRevision` is the tour's.
export async function adminReorderSteps(tourId: number, stepIds: number[], expectedRevision?: ExpectedRevision) {
  return adminSend<{ ok: boolean; count: number; revision: number }>(
    'PATCH',
    `/api/admin/tours/${tourId}/steps/reorder`,
    withRevision({ step_ids: stepIds }, expectedRevision),
    'reorder steps failed',
  )
}

// `expectedRevision` is the STEP's: the revision the editor was opened from or
// last saved. The response carries the step as saved and its new revision.
export async function adminUpdateStep(
  tourId: number,
  stepId: number,
  payload: Record<string, any>,
  expectedRevision?: ExpectedRevision,
) {
  return adminSend<{ ok: boolean; revision: number; step: TourStep }>(
    'PATCH',
    `/api/admin/tours/${tourId}/steps/${stepId}`,
    withRevision(payload, expectedRevision),
    'update step failed',
  )
}

// Scoped to the tour the editor shows: the server refuses (404) a step that
// does not belong to it, so a stale editor can't delete some other step.
// `expectedRevision` is the tour's (the delete changes its structure); DELETE
// has no body, so it travels as a query parameter.
export async function adminDeleteStep(tourId: number, stepId: number, expectedRevision?: ExpectedRevision) {
  const qs = hasRevision(expectedRevision) ? `?expected_revision=${expectedRevision}` : ''
  return adminSend<{ ok: boolean; revision: number }>(
    'DELETE',
    `/api/admin/tours/${tourId}/steps/${stepId}${qs}`,
    undefined,
    'delete step failed',
  )
}

// Append one pin / shape to a step atomically on the server ("Copy to next
// step"). No expected revision: appends commute, so racing copies — from this
// tab or any other — all land, which a read-modify-write of the whole list
// could not guarantee. An empty or already-used `id` is replaced by a fresh
// one; the response carries the id used, the step's new revision and the
// updated step.
export type AppendAnnotationResult = { ok: boolean; id: string; revision: number; step: TourStep }

async function appendAnnotation(tourId: number, stepId: number, kind: 'pins' | 'shapes', item: PinItem | Shape) {
  return adminSend<AppendAnnotationResult>(
    'POST',
    `/api/admin/tours/${tourId}/steps/${stepId}/${kind}`,
    item,
    'append annotation failed',
  )
}

export function adminAppendStepPin(tourId: number, stepId: number, pin: PinItem) {
  return appendAnnotation(tourId, stepId, 'pins', pin)
}

export function adminAppendStepShape(tourId: number, stepId: number, shape: Shape) {
  return appendAnnotation(tourId, stepId, 'shapes', shape)
}

// ===== Admin: tour assets (images for Markdown) =====

// max_bytes is the server's per-file upload cap (TOUR_ASSET_MAX_BYTES).
export async function adminListTourAssets(tourId: number, opts: { signal?: AbortSignal } = {}) {
  return adminGet<{ items: { name: string; url: string }[]; max_bytes: number }>(
    `/api/admin/tours/${tourId}/assets`,
    'list assets failed',
    { signal: opts.signal },
  )
}

// maxBytes: the server's cap as adminListTourAssets reported it, to refuse an
// oversize file without sending it; null when not known (the server decides).
export async function adminUploadTourAsset(
  tourId: number,
  file: File,
  duplicatePolicy: 'rename' | 'overwrite' = 'rename',
  maxBytes: number | null = null,
) {
  if (maxBytes != null && file.size > maxBytes) throw new FileTooLargeError()
  const form = new FormData()
  form.append('file', file)
  form.append('duplicate_policy', duplicatePolicy)
  return adminSend<{ ok: boolean; name: string; url: string; md: string; duplicate_policy: 'rename' | 'overwrite' }>(
    'POST',
    `/api/admin/tours/${tourId}/assets/upload`,
    form,
    'upload asset failed',
  )
}

export async function adminDeleteTourAsset(tourId: number, name: string) {
  return adminSend(
    'DELETE',
    `/api/admin/tours/${tourId}/assets/${encodeURIComponent(name)}`,
    undefined,
    'delete asset failed',
  )
}
