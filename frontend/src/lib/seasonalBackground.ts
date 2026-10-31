import type { SeasonalBackground } from './api'

function dayOfYearOrdinal(month: number, day: number): number {
  return month * 100 + day
}

export function isDateInRange(
  today: Date,
  startMonth: number,
  startDay: number,
  endMonth: number,
  endDay: number,
): boolean {
  const s = dayOfYearOrdinal(startMonth, startDay)
  const e = dayOfYearOrdinal(endMonth, endDay)
  // Year-wrap is disallowed: callers split such windows into two slots.
  // Rows that violate this are treated as inactive rather than silently
  // activated.
  if (s > e) return false
  const t = dayOfYearOrdinal(today.getMonth() + 1, today.getDate())
  return t >= s && t <= e
}

export type HeaderState = {
  imageUrl: string
  titleColor: string
}

/**
 * Resolve the active header background+color, walking the priority chain:
 *   highest-numbered matching seasonal slot > always-on default > none.
 *
 * Each field is resolved independently (a slot with only a color set
 * doesn't blank out the default image), which matches the admin UI's
 * presentation where image and color are separate inputs.
 */
export function pickActiveHeaderState(
  today: Date,
  list: SeasonalBackground[] | undefined,
  defaults: { imageUrl: string; titleColor: string },
): HeaderState {
  if (!list || list.length === 0) {
    return { imageUrl: defaults.imageUrl, titleColor: defaults.titleColor }
  }
  for (let i = list.length - 1; i >= 0; i--) {
    const sb = list[i]
    if (!sb.enabled) continue
    if (!isDateInRange(today, sb.start_month, sb.start_day, sb.end_month, sb.end_day)) continue
    const imageUrl = sb.has_image && sb.image_url ? sb.image_url : defaults.imageUrl
    const titleColor = sb.title_color || defaults.titleColor
    return { imageUrl, titleColor }
  }
  return { imageUrl: defaults.imageUrl, titleColor: defaults.titleColor }
}
