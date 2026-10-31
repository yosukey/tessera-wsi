import React, { useState } from 'react'
import { Slide, TourSummary, adminPublishSlide, adminSetSlideUnlisted, adminPreviewLinkProps, isManagerOrAboveFromToken, tourConflictOf, tourVisibilityApi } from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { logger } from '../../lib/logger'
import { cssFilterString, imageAdjustFromSlide, isImageAdjustDefault } from '../../lib/imageAdjust'
import { Visibility, VisibilityError, applyVisibility, visibilityOf } from '../../lib/visibility'
import { Button, Card, LinkButton, VisibilitySelect } from '../ui'
import { formatMpp, formatBytes, displayFilename, META_DISPLAY_KEYS } from '../../lib/admin/validation'
import { formatInEmbeddedOffset } from '../../lib/admin/datetime'
import { apiErrorMessage } from '../../lib/admin/apiError'
import ImageAdjustIcon from '../icons/ImageAdjustIcon'
import UrlShareControl from './UrlShareControl'
import { attributeParts } from '../../lib/attributes'

// A tour's own attributes as one line: the base categories in their usual
// order, then any other category alphabetically, like the slide's line.
function tourAttributeSummary(attrs: Record<string, string[]> | null | undefined): string {
  if (!attrs) return ''
  const rest = Object.keys(attrs).filter((k) => !META_DISPLAY_KEYS.includes(k)).sort()
  return attributeParts(attrs, [...META_DISPLAY_KEYS, ...rest]).join(' · ')
}

type Props = {
  slides: Slide[]
  slideTours: Record<number, TourSummary[]>
  selected: Set<number>
  onToggleSelect: (slideId: number) => void
  // Tours are selected separately from slides (see TourBulkActionBar).
  selectedTours: Set<number>
  onToggleSelectTour: (tourId: number) => void
  onOpenEdit: (slide: Slide) => void
  onDeleteSlide: (slideId: number, title: string) => void
  onCreateTour: (slideId: number) => void
  onDuplicateTour: (tour: TourSummary) => void
  // A click on a link that leaves the dashboard in this tab (a tour's
  // editor): the dashboard confirms what leaving would drop and navigates
  // through its navigation guard.
  onLinkClick: (e: React.MouseEvent<HTMLAnchorElement>, to: string) => void
  onPatchSlideLocal: (slideId: number, patch: Partial<Slide>) => void
  onPatchTourLocal: (slideId: number, tourId: number, patch: Partial<TourSummary>) => void
  // Asks before a slide returns to draft (its published tours follow it):
  // resolves whether to go ahead. The dashboard checks the tours afresh when
  // its list failed to load.
  onConfirmUnpublish: (slideId: number) => Promise<boolean>
  // The slide's published tours were cascaded to draft server-side.
  onToursDrafted: (slideId: number) => void
}

function SlideList({
  slides, slideTours, selected, onToggleSelect, selectedTours, onToggleSelectTour, onOpenEdit, onDeleteSlide, onCreateTour, onDuplicateTour, onLinkClick,
  onPatchSlideLocal, onPatchTourLocal, onConfirmUnpublish, onToursDrafted,
}: Props) {
  const { t } = useI18n()
  const canManageSlide = isManagerOrAboveFromToken()
  // Surface publish-toggle failures to the user rather than only logging to
  // the console, which leaves the toggle visually un-flipped with no explanation.
  const [errMsg, setErrMsg] = useState('')
  // Optimistic intent maps so the segmented control visually moves to the
  // chosen level immediately on click and stays disabled until the server
  // responds. Without these, every click would incur a multi-request refresh
  // round trip before any UI feedback, which (a) would make the control look
  // stuck and (b) would let the user queue more clicks that compound into
  // nginx 429 rate-limit errors. The map value is the target visibility;
  // presence of a key means a change is in flight (so the control reads from
  // here instead of the server-truth prop and disables itself).
  const [pendingSlideVis, setPendingSlideVis] = useState<Map<number, Visibility>>(new Map())
  const [pendingTourVis, setPendingTourVis] = useState<Map<number, Visibility>>(new Map())

  const setSlideVisPending = (id: number, intent: Visibility | undefined) => {
    setPendingSlideVis((prev) => {
      const next = new Map(prev)
      if (intent === undefined) next.delete(id)
      else next.set(id, intent)
      return next
    })
  }
  const setTourVisPending = (id: number, intent: Visibility | undefined) => {
    setPendingTourVis((prev) => {
      const next = new Map(prev)
      if (intent === undefined) next.delete(id)
      else next.set(id, intent)
      return next
    })
  }

  const visLabels: Record<Visibility, string> = {
    draft: t('adminDash.visDraft'),
    unlisted: t('adminDash.visLinkOnly'),
    public: t('adminDash.visPublic'),
  }

  // Resync a slide/tour row to whatever actually committed on the server
  // before a partial failure, so a half-applied change (e.g. unlisted flipped
  // but the following publish rejected) doesn't leave a stale flag that
  // mis-drives the NEXT visibility decision.
  const resyncSlideFromError = (slideId: number, err: unknown) => {
    if (err instanceof VisibilityError) {
      onPatchSlideLocal(slideId, {
        is_published: err.partial.is_published,
        unlisted: err.partial.unlisted,
      })
    }
  }
  // For tours, `revision` is the tour revision as of the failure (calls that
  // committed before it advanced it). A revision conflict — the row was
  // stale because the tour changed in the editor or another window — carries
  // the server's current tour instead, which beats any partial state.
  const resyncTourFromError = (slideId: number, tourId: number, err: unknown, revision: number | null | undefined) => {
    const current = tourConflictOf(err instanceof VisibilityError ? err.cause : err)
    if (current) {
      onPatchTourLocal(slideId, tourId, {
        is_published: current.tour.is_published,
        unlisted: current.tour.unlisted,
        revision: current.tour.revision,
      })
    } else if (err instanceof VisibilityError) {
      onPatchTourLocal(slideId, tourId, {
        is_published: err.partial.is_published,
        unlisted: err.partial.unlisted,
        revision,
      })
    }
  }

  // applyVisibility wraps failures in a VisibilityError whose message is the
  // raw cause message — for a coded API error that is the untranslated i18n
  // key — so translate the underlying cause instead.
  const visibilityErrorText = (err: unknown) =>
    apiErrorMessage(err instanceof VisibilityError ? err.cause : err, t) || t('adminDash.publishFailed')

  // A tour's visibility change is out on this slide. The slide's own waits for
  // it (as a tour's waits for its slide's): a slide to draft cascades its
  // tours to draft, so the two answers could land in either order.
  const slideToursPending = (slideId: number) =>
    (slideTours[slideId] || []).some((tour) => pendingTourVis.has(tour.id))

  async function changeSlideVisibility(s: Slide, target: Visibility) {
    const current = visibilityOf(s)
    if (target === current || pendingSlideVis.has(s.id) || slideToursPending(s.id)) return
    setErrMsg('')
    // Pending from here on, so the row stays disabled while the tours are
    // checked for the confirmation below.
    setSlideVisPending(s.id, target)
    try {
      // Returning a slide to draft cascades its published tours to draft
      // server-side; warn first so a slip doesn't silently unpublish them.
      if (target === 'draft' && !(await onConfirmUnpublish(s.id))) return
      const r = await applyVisibility({ is_published: s.is_published, unlisted: s.unlisted }, target, {
        publish: (v) => adminPublishSlide(s.id, v),
        setUnlisted: (v) => adminSetSlideUnlisted(s.id, v),
      })
      onPatchSlideLocal(s.id, { is_published: r.is_published, unlisted: r.unlisted })
      // Mirror the server cascade locally so the tour rows don't read
      // "published" until a refetch lands.
      if (target === 'draft' && r.cascaded_tours > 0) onToursDrafted(s.id)
    } catch (err) {
      logger.error('Failed to change slide visibility:', err)
      resyncSlideFromError(s.id, err)
      setErrMsg(visibilityErrorText(err))
    } finally {
      setSlideVisPending(s.id, undefined)
    }
  }

  async function changeTourVisibility(s: Slide, tour: TourSummary, target: Visibility) {
    const current = visibilityOf(tour)
    if (target === current || pendingTourVis.has(tour.id) || pendingSlideVis.has(s.id)) return
    setErrMsg('')
    // Expects the tour revision this row was listed at, threaded across
    // applyVisibility's calls; a 409 (the tour changed in the editor or
    // another window) resyncs the row from the server's copy.
    const tourVis = tourVisibilityApi(tour.id, tour.revision)
    const tourApi = tourVis.api
    // Making a tour link-only or public requires its parent slide to be
    // published; when the slide is still a draft, offer to publish it too
    // (otherwise the server 409s and the tour would be unreachable). Publish
    // the parent at the SAME visibility the admin chose for the tour: a
    // link-only tour publishes its parent link-only too, so the parent slide
    // never lands on the public top page — the exposure link-only exists to
    // avoid. A public tour still publishes its parent public.
    if (target !== 'draft' && !s.is_published) {
      if (!confirm(t('adminDash.tourPublishParentDraftConfirm'))) return
      setSlideVisPending(s.id, target)
      setTourVisPending(tour.id, target)
      const slideApi = {
        publish: (v: boolean) => adminPublishSlide(s.id, v),
        setUnlisted: (v: boolean) => adminSetSlideUnlisted(s.id, v),
      }
      let slidePatched = false
      try {
        const rs = await applyVisibility({ is_published: s.is_published, unlisted: s.unlisted }, target, slideApi)
        onPatchSlideLocal(s.id, { is_published: rs.is_published, unlisted: rs.unlisted })
        slidePatched = true
        const rt = await applyVisibility({ is_published: tour.is_published, unlisted: tour.unlisted }, target, tourApi)
        onPatchTourLocal(s.id, tour.id, { is_published: rt.is_published, unlisted: rt.unlisted, revision: tourVis.revision() })
      } catch (err) {
        logger.error('Failed to publish tour with parent slide:', err)
        // The failure is in the slide stage until its patch lands, then in the
        // tour stage; resync whichever one actually failed part-way.
        if (!slidePatched) resyncSlideFromError(s.id, err)
        else resyncTourFromError(s.id, tour.id, err, tourVis.revision())
        setErrMsg(visibilityErrorText(err))
      } finally {
        setSlideVisPending(s.id, undefined)
        setTourVisPending(tour.id, undefined)
      }
      return
    }
    setTourVisPending(tour.id, target)
    try {
      const r = await applyVisibility({ is_published: tour.is_published, unlisted: tour.unlisted }, target, tourApi)
      onPatchTourLocal(s.id, tour.id, { is_published: r.is_published, unlisted: r.unlisted, revision: tourVis.revision() })
    } catch (err) {
      logger.error('Failed to change tour visibility:', err)
      resyncTourFromError(s.id, tour.id, err, tourVis.revision())
      setErrMsg(visibilityErrorText(err))
    } finally {
      setTourVisPending(tour.id, undefined)
    }
  }

  if (slides.length === 0) {
    return (
      <div style={{ padding: '32px 12px', textAlign: 'center', color: 'var(--color-text-subtle)', fontSize: 'var(--font-size-sm)' }}>
        {t('adminDash.noSlidesMatch')}
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {errMsg && (
        <div
          role="alert"
          style={{
            fontSize: 12,
            padding: '6px 10px',
            borderRadius: 4,
            background: 'var(--color-danger-bg)',
            color: 'var(--color-danger-fg)',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <span style={{ flex: 1 }}>{errMsg}</span>
          <button
            type="button"
            onClick={() => setErrMsg('')}
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'inherit', padding: 0, fontSize: 14 }}
          >×</button>
        </div>
      )}
      {slides.map(s => {
        const tours = slideTours[s.id] || []
        const baseAttrs = META_DISPLAY_KEYS
          .map(k => ((s.attributes || {})[k] || []).join(', '))
          .filter(Boolean)
          .join(' · ')
        const ext = s.wsi_filename?.match(/\.([^.]+)$/)?.[1]?.toUpperCase() ?? ''
        const mppText = (s.mpp_x || s.mpp_y) ? formatMpp(s.mpp_x, s.mpp_y) : t('adminDash.mppMissing')
        const mppMissing = !(s.mpp_x || s.mpp_y)
        // Publishing without a catalog_id / title surfaces the slide on the
        // public listing as a bare "Slide #N" — confusing for viewers and
        // effectively unpublishable content. Block the toggle (and surface
        // why on hover) until the admin fills both in via the edit modal.
        const missingPublishFields = !s.catalog_id?.trim() || !s.title?.trim()
        const publishBlocked = missingPublishFields && !s.is_published

        return (
          <Card key={s.id} row padding="none">
            <div style={{ display: 'flex', alignItems: 'center', padding: '0 8px', flexShrink: 0, alignSelf: 'stretch' }}>
              <input
                type="checkbox"
                checked={selected.has(s.id)}
                onChange={() => onToggleSelect(s.id)}
                aria-label={t('adminDash.bulkSelectSlide', { id: s.id })}
                style={{ margin: 0, cursor: 'pointer' }}
              />
            </div>
            <div style={{ position: 'relative', width: 120, height: 80, flexShrink: 0, background: 'var(--color-surface-muted)', alignSelf: 'flex-start' }}>
              {s.thumb_url && (
                <img src={s.thumb_url} alt={s.title} style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block', filter: cssFilterString(imageAdjustFromSlide(s)) }} />
              )}
              {!isImageAdjustDefault(imageAdjustFromSlide(s)) && (
                <span
                  title={t('adminDash.imageAdjustActive')}
                  aria-label={t('adminDash.imageAdjustActive')}
                  style={{
                    position: 'absolute',
                    top: 4,
                    right: 4,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    width: 20,
                    height: 20,
                    borderRadius: 'var(--radius-md, 6px)',
                    background: 'rgba(0, 0, 0, 0.6)',
                    color: '#fff',
                  }}
                >
                  <ImageAdjustIcon size={13} />
                </span>
              )}
            </div>
            <div style={{ padding: '8px 12px', flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
              {/* Row 1: slide info */}
              <div style={{ minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, minWidth: 0 }}>
                  {s.catalog_id && (
                    <span style={{ fontSize: 'var(--font-size-md)', color: 'var(--color-text)', letterSpacing: '0.03em', flexShrink: 0 }}>{s.catalog_id}</span>
                  )}
                  <span style={{ fontWeight: 600, fontSize: 'var(--font-size-md)', color: 'var(--color-text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {s.title || t('adminDash.slideFallback', { id: s.id })}
                  </span>
                  <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-faint)', flexShrink: 0 }}>#{s.id}</span>
                  {tours.length > 0 && (
                    <span style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)', flexShrink: 0 }}>· {t('adminDash.rowToursInline', { count: tours.length })}</span>
                  )}
                </div>
              </div>

              {/* Row 2: action buttons */}
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', justifyContent: 'flex-end', alignItems: 'center' }}>
                <VisibilitySelect
                  size="sm"
                  ariaLabel={t('adminDash.visLabel')}
                  value={pendingSlideVis.get(s.id) ?? visibilityOf(s)}
                  labels={visLabels}
                  disabled={pendingSlideVis.has(s.id) || slideToursPending(s.id)}
                  publishDisabled={publishBlocked}
                  publishDisabledTitle={t('adminDash.publishBlockedMissingFields')}
                  linkOnlyTitle={t('adminDash.linkOnlyHint')}
                  onChange={(target) => changeSlideVisibility(s, target)}
                />
                <LinkButton
                  size="sm"
                  variant="secondary"
                  to={`/slides/${s.public_id}?from=admin`}
                  {...adminPreviewLinkProps}
                  title={s.is_published ? undefined : t('adminDash.openViewerDraftHint')}
                >
                  {s.is_published ? t('adminDash.openViewer') : t('adminDash.openViewerDraft')}
                </LinkButton>
                <UrlShareControl path={`/slides/${s.public_id}`} downloadName={`slide-${s.id}`} mode="popover" />
                {canManageSlide && (
                  <>
                    <Button size="sm" variant="secondary" onClick={() => onOpenEdit(s)}>{t('adminDash.editMetadataBtn')}</Button>
                    <Button size="sm" variant="danger" onClick={() => onDeleteSlide(s.id, s.title || t('adminDash.slideFallback', { id: s.id }))}>{t('adminDash.deleteSlide')}</Button>
                  </>
                )}
              </div>

              {/* Metadata details */}
              <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-subtle)', display: 'flex', flexWrap: 'wrap', gap: 10, minWidth: 0 }}>
                {s.vendor && <span>{s.vendor}</span>}
                {ext && <span>{ext}</span>}
                {(s.width > 0 || s.height > 0) && (
                  <span>{s.width.toLocaleString()} × {s.height.toLocaleString()} px</span>
                )}
                <span style={{ color: mppMissing ? 'var(--color-warning-fg)' : undefined }}>
                  {t('adminDash.rowMpp')}: {mppText}
                </span>
                {(s.file_size_bytes ?? 0) > 0 && (
                  <span>{formatBytes(s.file_size_bytes || null)}</span>
                )}
                {s.wsi_filename && (
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 280 }} title={s.wsi_filename}>
                    {displayFilename(s.wsi_filename)}
                  </span>
                )}
                {s.created_at && (() => {
                  const date = formatInEmbeddedOffset(s.created_at, { dateOnly: true })
                  if (date === '—') return null
                  return <span title={formatInEmbeddedOffset(s.created_at)}>{t('adminDash.rowUploadedAt')}: {date}</span>
                })()}
              </div>
              {baseAttrs && (
                <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {baseAttrs}
                </div>
              )}

              {/* Tours section */}
              {tours.length > 0 && (
                // minmax(0, 1fr): an implicit grid column is sized to its
                // rows' min-content width, so a row wider than the card would push
                // the column — and every row in it — off the screen.
                <div style={{ marginTop: 4, display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 2, paddingLeft: 'var(--space-3)' }}>
                  <div style={{ fontSize: 'var(--font-size-sm)', fontWeight: 600, color: 'var(--color-text-muted)', marginBottom: 2 }}>
                    {t('adminDash.toursSectionTitle')}
                  </div>
                  {tours.map(tour => (
                    // Wraps on a narrow screen (a portrait tablet): the
                    // controls drop below the title as a right-aligned group,
                    // and wrap among themselves if even a line of their own
                    // is too narrow, rather than running off the card.
                    <div key={tour.id} style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center', fontSize: 'var(--font-size-sm)', border: '1px solid var(--color-border)', borderRadius: 'var(--radius-md)', padding: '6px 8px' }}>
                      <div style={{ flex: '1 1 160px', minWidth: 0, display: 'flex', gap: 6, alignItems: 'center' }}>
                        <input
                          type="checkbox"
                          checked={selectedTours.has(tour.id)}
                          onChange={() => onToggleSelectTour(tour.id)}
                          aria-label={t('adminDash.bulkSelectTour', { id: tour.id })}
                          style={{ margin: 0, cursor: 'pointer', flexShrink: 0 }}
                        />
                        <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 600, color: 'var(--color-text)' }}>{tour.title}</span>
                        <span style={{ opacity: 0.5, flexShrink: 0 }}>#{tour.id}</span>
                      </div>
                      <div style={{ marginLeft: 'auto', minWidth: 0, display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', gap: 6, alignItems: 'center' }}>
                        <VisibilitySelect
                          size="sm"
                          ariaLabel={t('adminDash.visLabel')}
                          value={pendingTourVis.get(tour.id) ?? visibilityOf(tour)}
                          labels={visLabels}
                          disabled={pendingTourVis.has(tour.id) || pendingSlideVis.has(s.id)}
                          publishDisabled={!tour.is_published && missingPublishFields}
                          publishDisabledTitle={t('adminDash.publishBlockedMissingFields')}
                          linkOnlyTitle={t('adminDash.linkOnlyHint')}
                          onChange={(target) => changeTourVisibility(s, tour, target)}
                        />
                        <LinkButton
                          size="sm"
                          variant="secondary"
                          to={`/slides/${tour.slide_public_id}/tours/${tour.public_id}?from=admin`}
                          {...adminPreviewLinkProps}
                          title={tour.is_published ? undefined : t('adminDash.openViewerDraftHint')}
                        >
                          {tour.is_published ? t('adminDash.tourOpen') : t('adminDash.tourOpenDraft')}
                        </LinkButton>
                        <LinkButton
                          size="sm"
                          variant="secondary"
                          to={`/admin/tours/${tour.id}`}
                          onClick={(e) => onLinkClick(e, `/admin/tours/${tour.id}`)}
                        >
                          {t('adminDash.tourEdit')}
                        </LinkButton>
                        <Button size="sm" variant="secondary" onClick={() => onDuplicateTour(tour)} title={t('adminDash.tourDuplicateHint')}>
                          {t('adminDash.tourDuplicate')}
                        </Button>
                      </div>
                      {(() => {
                        // The tour's own metadata (what it inherits is the
                        // slide's, shown above), on a line of its own.
                        const own = tourAttributeSummary(tour.attributes)
                        return own ? (
                          <div title={t('adminTour.metadataOwn')} style={{ flexBasis: '100%', minWidth: 0, color: 'var(--color-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {own}
                          </div>
                        ) : null
                      })()}
                    </div>
                  ))}
                </div>
              )}

              {/* New tour button */}
              <div style={{ marginTop: 4, display: 'flex', justifyContent: 'flex-end' }}>
                <Button size="sm" variant="primary" onClick={() => onCreateTour(s.id)}>{t('adminDash.newTour')}</Button>
              </div>
            </div>
          </Card>
        )
      })}
    </div>
  )
}

// Memoised: the dashboard re-renders on every keystroke in its search box (and
// on unrelated state such as a modal opening), and re-rendering thousands of
// rows each time would cause the typing lag. Its props are data plus
// stable callbacks. (Toggling a row's checkbox still re-renders the rows: the
// `selected` / `selectedTours` set is a new object each time.)
export default React.memo(SlideList)
