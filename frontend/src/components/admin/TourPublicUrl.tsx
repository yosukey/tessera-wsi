import React from 'react'
import UrlShareControl from './UrlShareControl'

export default function TourPublicUrl(
  { slidePublicId, tourPublicId, stepNumber }: { slidePublicId: string; tourPublicId: string; stepNumber?: number },
) {
  // The public tour URL is keyed by the slide/tour public_id tokens. When a
  // step is selected in the editor, append ?step=N (1-based) so the shared link
  // opens directly at that step. Step 1 is the tour's start, so it's left off to
  // keep the canonical tour URL — matching the public viewer, which drops the
  // param for the first step.
  const hasStep = typeof stepNumber === 'number' && stepNumber > 1
  // Encode both tokens, matching every other URL-building call site (ViewerPage,
  // SlideSwitcher, useSyncFollower). Server-minted public_ids are already
  // URL-safe, so this is defence in depth rather than a live fix — but leaving
  // the one call site unencoded is how a later change to the token alphabet
  // turns into a silently wrong share link and QR code.
  const path = `/slides/${encodeURIComponent(slidePublicId)}/tours/${encodeURIComponent(tourPublicId)}${hasStep ? `?step=${stepNumber}` : ''}`
  const downloadName = hasStep ? `tour-${tourPublicId}-step${stepNumber}` : `tour-${tourPublicId}`
  return <UrlShareControl path={path} downloadName={downloadName} mode="inline" />
}
