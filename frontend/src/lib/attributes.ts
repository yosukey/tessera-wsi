export type AttributeMap = Record<string, string[]>

// A tour's effective metadata: its slide's plus its own, per key, the slide's
// values first and each value once. Either side may be missing (a slide
// still loading).
export function mergeAttributes(
  slide: AttributeMap | null | undefined,
  tour: AttributeMap | null | undefined,
): AttributeMap {
  const out: AttributeMap = {}
  for (const src of [slide, tour]) {
    for (const [key, values] of Object.entries(src || {})) {
      const acc = out[key] ?? (out[key] = [])
      for (const v of values || []) if (!acc.includes(v)) acc.push(v)
    }
  }
  return out
}

// The values of `keys`, in that order, as display parts: one per key that has
// any, its values joined with ", ".
export function attributeParts(attrs: AttributeMap, keys: string[]): string[] {
  return keys.map((k) => (attrs[k] || []).join(', ')).filter(Boolean)
}
