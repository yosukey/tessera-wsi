// Tour-step framing: the conversion between the live OSD viewport and a
// step's stored (center, FOV in µm, rotation). The editor's "apply current
// view" (capture) and every place that frames a step (editor jump / live
// edit sync, public tour viewer) go through this pair so they stay exact
// inverses of each other.
//
// Stored FOV semantics: fov_um_w × fov_um_h is the *screen-aligned* field
// that was visible when the step was captured, i.e. the viewport before
// rotation (getBoundsNoRotate). Framing therefore hands fitBounds that rect
// rotated by -rotation: OSD's fitBounds adds the viewport rotation back and
// fits the resulting bounding box, which is then the rect itself. Passing
// the unrotated rect instead would make OSD fit its rotated bounding box, zooming
// out by up to ~2x at oblique angles.
//
// Stored FOV units: the screen-aligned width/height in level-0 pixels, each
// multiplied by ONE reference factor, stepMpp(slide). For a slide with square
// pixels — the normal case, and also a slide with only one or no axis
// calibrated — that is exactly the physical size in µm. For an anisotropic
// slide (mpp_x != mpp_y) it is not: OSD draws every image pixel square, so at
// rotation θ the screen-horizontal side runs along image direction
// (cos θ, ±sin θ) and is w_px · hypot(mpp_x·cos θ, mpp_y·sin θ) µm long, the
// vertical side h_px · hypot(mpp_x·sin θ, mpp_y·cos θ) µm.
//
// The persisted units are deliberately NOT per-axis physical µm.
// Framing fits BOTH dimensions (fitBounds keeps the whole rect visible on any
// screen shape), so reinterpreting the stored height — and under rotation the
// width — would silently re-frame every step already saved on an anisotropic
// slide, and a step row carries no format version to tell old rows from new
// ones. A single reference factor also means that correcting a slide's mpp_y
// later never moves its tours. Instead the editor converts at its boundary:
// it shows and accepts physical µm (fovToPhysical / fovFromPhysical, which
// are rotation-aware), while capture, framing, copy/duplicate and the
// database all stay in reference units. For square pixels the conversion is
// exactly the identity, so nothing changes there.
import OpenSeadragon from 'openseadragon'
import { slidePixelSize } from './scalebar'

// Slides without a recorded MPP still need a µm <-> px factor, and it must be
// the same one on capture and on framing, or a captured view can never be
// reproduced. At 0.5 µm/px a 600×450 µm field of view frames 1200×900 px.
export const FALLBACK_MPP = 0.5

type SlideMpp = { mpp_x?: number | null; mpp_y?: number | null }

// The reference factor of the stored FOV units (see the header): mpp_x, else
// mpp_y, else FALLBACK_MPP. Used for both axes on capture and on framing.
export function stepMpp(slide: SlideMpp): number {
  const mpp = slide.mpp_x || slide.mpp_y || 0
  return mpp > 0 ? mpp : FALLBACK_MPP
}

export type StepFrame = {
  center_x: number
  center_y: number
  fov_um_w: number
  fov_um_h: number
  rotation_deg?: number | null
}

export type FovSize = { w: number; h: number }

// Multipliers taking a stored (reference-unit) FOV to physical µm at
// `rotationDeg`. Exactly {1, 1} for square pixels, so those round-trip
// bit-for-bit.
function physicalScale(slide: SlideMpp, rotationDeg: number): FovSize {
  const px = slidePixelSize(slide)
  if (!px || px.x === px.y) return { w: 1, h: 1 }
  const ref = stepMpp(slide)
  const t = ((Number.isFinite(rotationDeg) ? rotationDeg : 0) * Math.PI) / 180
  const c = Math.cos(t)
  const s = Math.sin(t)
  return {
    w: Math.hypot(px.x * c, px.y * s) / ref,
    h: Math.hypot(px.x * s, px.y * c) / ref,
  }
}

// Stored FOV -> the physical µm the editor displays.
export function fovToPhysical(slide: SlideMpp, rotationDeg: number, fov: FovSize): FovSize {
  const k = physicalScale(slide, rotationDeg)
  return { w: fov.w * k.w, h: fov.h * k.h }
}

// Physical µm typed / shown in the editor -> stored FOV. Exact inverse of
// fovToPhysical for the same rotation.
export function fovFromPhysical(slide: SlideMpp, rotationDeg: number, fov: FovSize): FovSize {
  const k = physicalScale(slide, rotationDeg)
  return { w: fov.w / k.w, h: fov.h / k.h }
}

// Viewport-coordinate rect to pass to fitBounds (after setRotation(rotation)).
// `frame` is in stored units and `mpp` is stepMpp(slide).
export function stepViewportBounds(
  viewport: OpenSeadragon.Viewport,
  frame: StepFrame,
  mpp: number,
): OpenSeadragon.Rect {
  const w = frame.fov_um_w / mpp
  const h = frame.fov_um_h / mpp
  const rect = new OpenSeadragon.Rect(frame.center_x - w / 2, frame.center_y - h / 2, w, h)
  const vpRect = viewport.imageToViewportRectangle(rect)
  const rot = frame.rotation_deg || 0
  // Rotate about the rect's own centre, which is therefore preserved.
  return rot ? vpRect.rotate(-rot) : vpRect
}

// Inverse of stepViewportBounds for the current (animated) viewport state.
// Returns the FOV in stored units; convert with fovToPhysical for display.
export function captureStepFrame(
  viewport: OpenSeadragon.Viewport,
  mpp: number,
): { rot: number; centerX: number; centerY: number; fovUmW: number; fovUmH: number } {
  const rot = viewport.getRotation(true) || 0
  // Derive the center from the viewport center point: it is the pivot of the
  // rotation, so it needs no correction.
  const imgCenter = viewport.viewportToImageCoordinates(viewport.getCenter(true))
  const imgRect = viewport.viewportToImageRectangle(viewport.getBoundsNoRotate(true))
  return {
    rot,
    centerX: imgCenter.x,
    centerY: imgCenter.y,
    fovUmW: imgRect.width * mpp,
    fovUmH: imgRect.height * mpp,
  }
}
