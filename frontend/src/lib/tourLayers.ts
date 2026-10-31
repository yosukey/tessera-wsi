import type { PinItem, Shape } from './api'

// Stacking-order ("z") model shared by the tour editor and both viewers.
//
// Pins (placed items: pin/circle/arrow) and shapes (drawings: rect/circle/
// freehand) live on ONE z scale so any annotation can be moved above or below
// any other. Higher z paints on top.
//
// Defaults encode "pins above shapes": a new shape gets z = 0 and a new pin
// gets z = LAYER_PIN_BASE, so with no explicit reordering pins always sit above
// shapes. Must match schemas.LAYER_PIN_BASE / the DB column defaults
// (step_pins.z = 1000000, step_shapes.z = 0) on the backend.
export const LAYER_PIN_BASE = 1_000_000

// On equal z, pins beat shapes — a deterministic tie-break that keeps the
// combined order total. Higher rank = on top.
export const LAYER_KIND_RANK: Record<AnnotationKind, number> = { shape: 0, pin: 1 }

export type AnnotationKind = 'pin' | 'shape'

export type OrderedAnnotation =
  | { kind: 'pin'; item: PinItem; index: number }
  | { kind: 'shape'; item: Shape; index: number }

// Combine pins + shapes into one list ordered bottom → top. Sort key:
// (z asc, kind-rank asc, original-array-index asc). The array-index tie-break
// makes the order total (no ambiguity), and preserves each item's insertion
// order within its own z/kind group.
export function orderAnnotations(pins: PinItem[], shapes: Shape[]): OrderedAnnotation[] {
  const entries: OrderedAnnotation[] = [
    ...shapes.map((item, index): OrderedAnnotation => ({ kind: 'shape', item, index })),
    ...pins.map((item, index): OrderedAnnotation => ({ kind: 'pin', item, index })),
  ]
  return entries.sort((a, b) => {
    const za = a.item.z
    const zb = b.item.z
    if (za !== zb) return za - zb
    const ra = LAYER_KIND_RANK[a.kind]
    const rb = LAYER_KIND_RANK[b.kind]
    if (ra !== rb) return ra - rb
    return a.index - b.index
  })
}

export type LayerMove = 'front' | 'back' | 'forward' | 'backward'

// Re-assign z = position for every annotation from a reordered combined list,
// producing new pins / shapes arrays. The ARRAY order is left untouched (so the
// editor's #N badges and card order stay stable) — only the z field changes.
// Re-packing to dense, unique integers is what makes "move one step" a clean
// swap on the next call: no ties to disambiguate.
function applyOrder(
  pins: PinItem[],
  shapes: Shape[],
  ordered: OrderedAnnotation[],
): { pins: PinItem[]; shapes_l0: Shape[] } {
  const pinZ = new Map<string, number>()
  const shapeZ = new Map<string, number>()
  ordered.forEach((entry, position) => {
    if (entry.kind === 'pin') pinZ.set(entry.item.id, position)
    else shapeZ.set(entry.item.id, position)
  })
  return {
    pins: pins.map((p) => ({ ...p, z: pinZ.get(p.id) ?? p.z })),
    shapes_l0: shapes.map((s) => ({ ...s, z: shapeZ.get(s.id) ?? s.z })),
  }
}

// Move one annotation (identified by kind + id) within the combined stack and
// return updated pins / shapes arrays. Works across kinds, so a shape can be
// pushed in front of a pin and vice-versa. No-op (returns the inputs) if the id
// isn't found.
export function reorderAnnotation(
  pins: PinItem[],
  shapes: Shape[],
  kind: AnnotationKind,
  id: string,
  move: LayerMove,
): { pins: PinItem[]; shapes_l0: Shape[] } {
  const ordered = orderAnnotations(pins, shapes)
  const pos = ordered.findIndex((e) => e.kind === kind && e.item.id === id)
  if (pos < 0) return { pins, shapes_l0: shapes }

  const next = ordered.slice()
  const [entry] = next.splice(pos, 1)
  let insertAt: number
  switch (move) {
    case 'front': insertAt = next.length; break        // very top
    case 'back': insertAt = 0; break                   // very bottom
    case 'forward': insertAt = Math.min(pos + 1, next.length); break
    case 'backward': insertAt = Math.max(pos - 1, 0); break
  }
  next.splice(insertAt, 0, entry)
  return applyOrder(pins, shapes, next)
}

// z for a newly-added pin: on top of everything (and never below the pin band),
// so a freshly-placed pin is immediately visible above existing annotations.
export function nextPinZ(pins: PinItem[], shapes: Shape[]): number {
  const zs = [...pins, ...shapes].map((a) => a.z)
  const max = zs.length ? Math.max(...zs) : LAYER_PIN_BASE - 1
  return Math.max(LAYER_PIN_BASE, max + 1)
}

// z for a newly-drawn shape: above existing shapes but kept below the pins, so
// the "pins above shapes" default survives even when a shape is drawn after a
// pin. When z values are dense with no gap under the lowest pin, it lands just
// below that pin (the kind/index tie-break keeps it deterministic).
export function nextShapeZ(pins: PinItem[], shapes: Shape[]): number {
  const shapeMax = shapes.length ? Math.max(...shapes.map((s) => s.z)) : -1
  const pinMin = pins.length ? Math.min(...pins.map((p) => p.z)) : Infinity
  const candidate = shapeMax + 1
  return pinMin === Infinity ? candidate : Math.min(candidate, pinMin - 1)
}
