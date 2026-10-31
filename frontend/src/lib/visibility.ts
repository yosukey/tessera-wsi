// Publish visibility as a single 3-level scale, covering the two independent
// booleans (is_published + unlisted) at the UI layer. The two flags encode
// three meaningful states — the fourth combination (draft + unlisted) has no
// user-visible effect since a draft is already hidden everywhere — so a single
// ordered scale is the model that actually matches reality:
//
//   draft     → hidden entirely (is_published = false)
//   unlisted  → published, reachable by direct link only (is_published, unlisted)
//   public    → published and listed in browse/search (is_published, !unlisted)
export type Visibility = 'draft' | 'unlisted' | 'public'

// Ordered least→most public, so callers can compare levels if needed.
export const VISIBILITY_ORDER: readonly Visibility[] = ['draft', 'unlisted', 'public']

export function visibilityOf(item: { is_published?: boolean; unlisted?: boolean }): Visibility {
  if (!item.is_published) return 'draft'
  return item.unlisted ? 'unlisted' : 'public'
}

// The publish / unlisted endpoint pair for one item (slide or tour). publish()
// returns the server-confirmed published flag (and, for slides, how many child
// tours were cascaded to draft); setUnlisted() returns the confirmed flag.
export type VisibilityApi = {
  publish: (published: boolean) => Promise<{ is_published: boolean; cascaded_tours?: number }>
  setUnlisted: (unlisted: boolean) => Promise<{ unlisted: boolean }>
}

export type VisibilityResult = {
  is_published: boolean
  unlisted: boolean
  cascaded_tours: number
}

// Thrown by applyVisibility when one of its calls fails PART-WAY through the
// sequence (e.g. setUnlisted(true) committed but the following publish() then
// 400s on a missing file). `partial` carries the server-confirmed flags as of
// the failure so the caller can patch its local state to match what actually
// committed, instead of leaving a stale flag that poisons the NEXT visibility
// decision (applyVisibility skips calls based on the flags handed to it).
export class VisibilityError extends Error {
  partial: VisibilityResult
  cause: unknown
  constructor(cause: unknown, partial: VisibilityResult) {
    super(cause instanceof Error ? cause.message : String(cause))
    this.name = 'VisibilityError'
    this.cause = cause
    this.partial = partial
  }
}

// Move an item from its current flags to `target` with the minimum number of
// calls, returning the resulting flags so the caller can patch local state.
//
// `current` is the item's actual { is_published, unlisted } — NOT a derived
// Visibility level. The distinction matters: a *draft* can still carry
// unlisted=true in the DB (e.g. it was pre-marked link-only, or unpublished
// from link-only), which the 'draft' level alone doesn't reveal. Reading the
// real flag is what lets a draft→public move know it must clear unlisted.
//
// Key ordering rule: when moving *into* the unlisted state, flip `unlisted`
// BEFORE publishing. Otherwise a draft→link-only transition would publish
// first (briefly listing the item on the public top page) and only then hide
// it — the exact exposure window link-only exists to avoid.
//
// On a mid-sequence failure it throws a VisibilityError carrying the
// confirmed-so-far flags so the caller can resync (see VisibilityError above).
export async function applyVisibility(
  current: { is_published?: boolean; unlisted?: boolean },
  target: Visibility,
  api: VisibilityApi,
): Promise<VisibilityResult> {
  let is_published = !!current.is_published
  let unlisted = !!current.unlisted
  let cascaded_tours = 0
  const snapshot = (): VisibilityResult => ({ is_published, unlisted, cascaded_tours })

  try {
    if (target === 'draft') {
      if (is_published) {
        const r = await api.publish(false)
        is_published = r.is_published
        cascaded_tours = r.cascaded_tours ?? 0
      }
      return snapshot()
    }

    const wantUnlisted = target === 'unlisted'
    if (unlisted !== wantUnlisted) {
      const r = await api.setUnlisted(wantUnlisted)
      unlisted = r.unlisted
    }
    if (!is_published) {
      const r = await api.publish(true)
      is_published = r.is_published
      cascaded_tours = r.cascaded_tours ?? 0
    }
    return snapshot()
  } catch (err) {
    throw new VisibilityError(err, snapshot())
  }
}
