export type ImageAdjust = {
  brightness: number
  contrast: number
  saturation: number
}

export const DEFAULT_IMAGE_ADJUST: ImageAdjust = {
  brightness: 1,
  contrast: 1,
  saturation: 1,
}

export const IMAGE_ADJUST_RANGES = {
  brightness: { min: 0.5, max: 1.5, step: 0.05 },
  contrast: { min: 0.5, max: 1.5, step: 0.05 },
  saturation: { min: 0, max: 2, step: 0.05 },
} as const

export function isImageAdjustDefault(v: ImageAdjust): boolean {
  return v.brightness === 1 && v.contrast === 1 && v.saturation === 1
}

export function imageAdjustEqual(a: ImageAdjust, b: ImageAdjust): boolean {
  return a.brightness === b.brightness && a.contrast === b.contrast && a.saturation === b.saturation
}

// Build the starting adjustment for a slide from its stored preset. NULL /
// missing channels fall back to identity (1.0), so a slide with no preset
// opens unadjusted.
export function imageAdjustFromSlide(s: {
  image_adjust_brightness?: number | null
  image_adjust_contrast?: number | null
  image_adjust_saturation?: number | null
} | null | undefined): ImageAdjust {
  return {
    brightness: s?.image_adjust_brightness ?? 1,
    contrast: s?.image_adjust_contrast ?? 1,
    saturation: s?.image_adjust_saturation ?? 1,
  }
}

// Returns 'none' for the identity adjustment so an untouched viewer doesn't
// pay for an extra GPU compositing layer.
export function cssFilterString(v: ImageAdjust): string {
  if (isImageAdjustDefault(v)) return 'none'
  return `brightness(${v.brightness}) contrast(${v.contrast}) saturate(${v.saturation})`
}
