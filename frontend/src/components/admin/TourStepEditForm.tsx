import React from 'react'
import SafeMarkdown from '../SafeMarkdown'
import ConflictBanner from './ConflictBanner'
import type { PinItem, PinShape, Shape, ShapeLabelPosition, ShapeKind, TransitionMode } from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import {
  PIN_COLORS,
  PIN_COLOR_ORDER,
  PIN_SHAPES,
  normalizeRotationDeg,
  resolvePinColor,
  contrastTextColor,
} from '../../lib/tourPins'
import {
  SHAPE_LABEL_POSITIONS,
  SHAPE_KINDS,
  normalizeShapeLabelPosition,
  normalizeShapeKind,
  resolveShapeColor,
} from '../../lib/tourShapes'
import type { EditState } from '../../lib/tourEditHelpers'
import { orderAnnotations, reorderAnnotation, type AnnotationKind, type LayerMove } from '../../lib/tourLayers'
import { FALLBACK_MPP } from '../../lib/tourStepView'

interface Props {
  editState: EditState
  setEditState: (next: EditState) => void
  hasNext: boolean
  addingPin: boolean
  setAddingPin: (v: boolean) => void
  pendingPinShape: PinShape | null
  setPendingPinShape: (v: PinShape | null) => void
  addingShape: ShapeKind | null
  setAddingShape: (v: ShapeKind | null) => void
  cancelDraft: () => void
  onApplyCurrentView: () => void
  onCopyPinToNext: (pin: PinItem) => void
  onCopyShapeToNext: (shape: Shape) => void
  // True while this step's save request is in flight: Save is disabled so a
  // second request can't overlap it. The fields stay editable; the editor
  // keeps anything changed meanwhile as unsaved (see AdminTourEditor.saveEdit).
  saving: boolean
  // True after a save was refused because this step changed in another window
  // or by another user since the editor loaded it. The fields keep the user's
  // edits; the banner offers to reload the server's version (discarding them)
  // or to save them over it. A plain Save conflicts again until one is chosen.
  conflict: boolean
  onReloadConflict: () => void
  onOverwriteConflict: () => void
  // The slide records no MPP, so the FOV fields are µm under an assumed
  // FALLBACK_MPP (lib/tourStepView), not a measured size: say so.
  fovEstimated: boolean
  onSave: () => void
  onCancel: () => void
}

// Accept CSS hex literals in 3/4/6/8 digit forms; the 4/8 digit forms
// carry an alpha channel (e.g. #11223344, #abcd) which the renderer
// passes through as opaque CSS. The native <input type="color"> picker
// doesn't surface alpha, but power users still get the alpha-hex flow
// via the text field on the per-shape/pin card.
function isHexColor(v: string): boolean {
  return /^#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(v.trim())
}

// Unicode glyphs used as compact shape-type icons in card headers.
const PIN_SHAPE_ICON: Record<PinShape, string> = { pin: '◆', circle: '●', arrow: '➤' }
const SHAPE_KIND_ICON: Record<ShapeKind, string> = { rect: '▭', circle: '○', freehand: '〜' }

// Mirrors services/.../schemas.py:_MAX_STEP_DESCRIPTION_LEN. Used as the
// textarea's hard cap so the editor refuses oversize input up front instead
// of round-tripping a 65k+ payload only to get a generic 422 back.
const STEP_DESCRIPTION_MAX_LEN = 65536

// Fieldset-style section wrapper with a small uppercase legend.
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <fieldset style={{
      border: '1px solid var(--color-border)',
      borderRadius: 10,
      padding: '4px 10px 10px',
      marginTop: 10,
    }}>
      <legend style={{
        fontSize: 10,
        fontWeight: 700,
        textTransform: 'uppercase',
        letterSpacing: '0.06em',
        padding: '0 4px',
        opacity: 0.55,
      }}>
        {title}
      </legend>
      {children}
    </fieldset>
  )
}

// Coloured pill that shows "#N". Same visual style as the on-canvas overlay badge.
function IndexBadge({ index, bgColor }: { index: number; bgColor: string }) {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        minWidth: 22,
        height: 20,
        padding: '0 5px',
        borderRadius: 999,
        background: bgColor,
        color: contrastTextColor(bgColor),
        fontSize: 11,
        fontWeight: 700,
        fontVariantNumeric: 'tabular-nums',
        lineHeight: 1,
        flexShrink: 0,
        boxShadow: '0 0 0 1.5px rgba(255,255,255,0.7) inset, 0 1px 2px rgba(0,0,0,0.18)',
      }}
    >
      #{index}
    </span>
  )
}

// Stacking-order controls shown in every annotation card header. The four
// buttons move the item within the shared pin+shape z stack. "To front" /
// "forward" disable when the item is already topmost; "backward" / "to back"
// disable when it is bottommost (so a lone annotation has all four disabled).
// Disabled <button>s are both non-interactive and visually dimmed by the UA.
function LayerControls({
  t,
  isTop,
  isBottom,
  onMove,
}: {
  t: (key: any, vars?: any) => string
  isTop: boolean
  isBottom: boolean
  onMove: (move: LayerMove) => void
}) {
  const btnStyle: React.CSSProperties = { fontSize: 11, lineHeight: 1, padding: '2px 5px' }
  return (
    <span style={{ display: 'inline-flex', gap: 2, flexShrink: 0 }}>
      <button type="button" style={btnStyle} disabled={isTop} title={t('adminTour.layer.toFront')} aria-label={t('adminTour.layer.toFront')} onClick={() => onMove('front')}>⤒</button>
      <button type="button" style={btnStyle} disabled={isTop} title={t('adminTour.layer.forward')} aria-label={t('adminTour.layer.forward')} onClick={() => onMove('forward')}>↑</button>
      <button type="button" style={btnStyle} disabled={isBottom} title={t('adminTour.layer.backward')} aria-label={t('adminTour.layer.backward')} onClick={() => onMove('backward')}>↓</button>
      <button type="button" style={btnStyle} disabled={isBottom} title={t('adminTour.layer.toBack')} aria-label={t('adminTour.layer.toBack')} onClick={() => onMove('back')}>⤓</button>
    </span>
  )
}

export default function TourStepEditForm({
  editState,
  setEditState,
  hasNext,
  addingPin,
  setAddingPin,
  pendingPinShape,
  setPendingPinShape,
  addingShape,
  setAddingShape,
  cancelDraft,
  onApplyCurrentView,
  onCopyPinToNext,
  onCopyShapeToNext,
  saving,
  conflict,
  onReloadConflict,
  onOverwriteConflict,
  fovEstimated,
  onSave,
  onCancel,
}: Props) {
  const { t } = useI18n()
  const labelHintId = React.useId()
  // A label is required to save (AdminTourEditor.saveEdit refuses a blank
  // one). Flagged from the moment the editor opens, so a step stored without
  // a label shows what it needs before Save is tried.
  const labelMissing = editState.label.trim() === ''
  const labelInputRef = React.useRef<HTMLInputElement>(null)
  // Save sits at the bottom of a possibly long form, out of sight of both the
  // label field and the editor message; bring the field into view (focusing
  // scrolls it there) when the save is about to be refused for it.
  const focusLabelIfMissing = () => {
    if (labelMissing) labelInputRef.current?.focus()
  }

  const updatePin = (pinId: string, patch: Partial<PinItem>) => {
    setEditState({
      ...editState,
      pins: editState.pins.map((p) => p.id === pinId ? { ...p, ...patch } : p),
    })
  }
  const removePin = (pinId: string) => {
    setEditState({ ...editState, pins: editState.pins.filter((p) => p.id !== pinId) })
  }
  const removeShape = (shapeId: string) => {
    setEditState({ ...editState, shapes_l0: editState.shapes_l0.filter((s) => s.id !== shapeId) })
  }
  const updateShape = (shapeId: string, patch: Partial<Shape>) => {
    setEditState({
      ...editState,
      shapes_l0: editState.shapes_l0.map((s) => s.id === shapeId ? { ...s, ...patch } : s),
    })
  }

  // Combined pin+shape stacking order (bottom → top), used to place the layer
  // buttons and to decide which are disabled at each end of the stack.
  const layerOrder = orderAnnotations(editState.pins, editState.shapes_l0)
  const layerCount = layerOrder.length
  const layerPos = new Map<string, number>()
  layerOrder.forEach((entry, i) => layerPos.set(`${entry.kind}:${entry.item.id}`, i))
  const moveAnnotation = (kind: AnnotationKind, id: string, move: LayerMove) => {
    const next = reorderAnnotation(editState.pins, editState.shapes_l0, kind, id, move)
    setEditState({ ...editState, pins: next.pins, shapes_l0: next.shapes_l0 })
  }

  const toggleAddingShape = (kind: ShapeKind) => {
    const next = addingShape === kind ? null : kind
    setAddingShape(next)
    cancelDraft()
    // Entering shape mode cancels any armed pin placement (setAddingShape
    // already clears addingPin in the hook); also drop the pending pin-shape
    // choice so the pin toolbar doesn't reopen pre-armed on the next click.
    if (next !== null) setPendingPinShape(null)
  }

  return (
    <div style={{ marginTop: 10 }} onClick={(e) => e.stopPropagation()}>

      {/* ── Viewport ─────────────────────────────────────────── */}
      <Section title={t('adminTour.section.viewport')}>
        <div style={{ display: 'grid', gap: 8, gridTemplateColumns: '1fr 1fr' }}>
          <div>
            <label style={{ fontSize: 12 }}>{t('adminTour.label')}
              <span aria-hidden="true" style={{ color: 'var(--color-danger-fg)', marginLeft: 2 }}>*</span>
              {/* Mirrors the Step.label column width the server enforces. */}
              <input
                ref={labelInputRef}
                value={editState.label}
                maxLength={255}
                required
                aria-invalid={labelMissing}
                aria-describedby={labelMissing ? labelHintId : undefined}
                onChange={(e) => setEditState({ ...editState, label: e.target.value })}
                style={{ width: '100%', ...(labelMissing ? { borderColor: 'var(--color-danger-fg)' } : {}) }}
              />
            </label>
            {labelMissing && (
              <div id={labelHintId} style={{ marginTop: 2, fontSize: 11, color: 'var(--color-danger-fg)' }}>
                {t('adminTour.labelRequired')}
              </div>
            )}
          </div>
          <label style={{ fontSize: 12 }}>{t('adminTour.rotation')}
            <input type="number" value={editState.rotation_deg} onChange={(e) => setEditState({ ...editState, rotation_deg: e.target.value })} style={{ width: '100%' }} />
          </label>
          <label style={{ fontSize: 12 }}>{t('adminTour.fovWidth')}
            <input type="number" value={editState.fov_um_w} onChange={(e) => setEditState({ ...editState, fov_um_w: e.target.value })} style={{ width: '100%' }} />
          </label>
          <label style={{ fontSize: 12 }}>{t('adminTour.fovHeight')}
            <input type="number" value={editState.fov_um_h} onChange={(e) => setEditState({ ...editState, fov_um_h: e.target.value })} style={{ width: '100%' }} />
          </label>
          {fovEstimated && (
            <div style={{ gridColumn: '1 / -1', fontSize: 11, color: 'var(--color-text-muted)' }}>
              {t('adminTour.fovEstimatedHint', { mpp: FALLBACK_MPP })}
            </div>
          )}
          <label style={{ fontSize: 12 }}>{t('adminTour.centerX')}
            <input type="number" value={editState.center_x} onChange={(e) => setEditState({ ...editState, center_x: e.target.value })} style={{ width: '100%' }} />
          </label>
          <label style={{ fontSize: 12 }}>{t('adminTour.centerY')}
            <input type="number" value={editState.center_y} onChange={(e) => setEditState({ ...editState, center_y: e.target.value })} style={{ width: '100%' }} />
          </label>
        </div>
        <div style={{ marginTop: 8 }}>
          <button type="button" onClick={onApplyCurrentView} title={t('adminTour.applyCurrentViewTitle')}>
            {t('adminTour.applyCurrentView')}
          </button>
        </div>
      </Section>

      {/* ── Transition ───────────────────────────────────────── */}
      <Section title={t('adminTour.section.transition')}>
        <label
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 4,
            fontSize: 12,
            opacity: hasNext ? 1 : 0.5,
            cursor: hasNext ? 'pointer' : 'not-allowed',
          }}
          title={hasNext ? t('adminTour.transitionModeTitle') : t('adminTour.transitionModeLastStep')}
        >
          <span>{t('adminTour.transitionMode')}</span>
          <select
            value={hasNext ? editState.transition_mode : 'none'}
            disabled={!hasNext}
            onChange={(e) =>
              setEditState({ ...editState, transition_mode: e.target.value as TransitionMode })
            }
            style={{ fontSize: 12, padding: '4px 6px' }}
          >
            <option value="none">{t('adminTour.transitionMode.none')}</option>
            <option value="smooth">{t('adminTour.transitionMode.smooth')}</option>
            <option value="smooth_zoom">{t('adminTour.transitionMode.smoothZoom')}</option>
          </select>
        </label>
      </Section>

      {/* ── Markers (Pins) ───────────────────────────────────── */}
      <Section title={t('adminTour.section.markers')}>
        {/* Add-pin toolbar */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          {addingPin ? (
            <>
              <span style={{ fontSize: 12, opacity: 0.75 }}>
                {t(`adminTour.pinShape.${pendingPinShape ?? 'pin'}`)}：{t('adminTour.clickCanvasToPlace')}
              </span>
              <button type="button" onClick={() => { setAddingPin(false); setPendingPinShape(null) }}>
                {t('adminTour.cancel')}
              </button>
            </>
          ) : (
            PIN_SHAPES.map((sh) => (
              <button
                key={sh}
                type="button"
                onClick={() => { setPendingPinShape(sh); setAddingPin(true) }}
              >
                {PIN_SHAPE_ICON[sh]} {t(`adminTour.pinShape.${sh}`)}
              </button>
            ))
          )}
        </div>

        {editState.pins.length === 0 && (
          <div style={{ fontSize: 12, opacity: 0.6, marginTop: 6 }}>{t('adminTour.none')}</div>
        )}

        {editState.pins.map((p, idx) => {
          const pinColor = resolvePinColor(p.color)
          const labelText = (p.label || '').trim()
          return (
            <div
              key={p.id}
              style={{
                border: '1px solid var(--color-border)',
                borderLeft: `4px solid ${pinColor}`,
                borderRadius: 8,
                marginTop: 8,
                overflow: 'hidden',
              }}
            >
              {/* Card header — quick fingerprint for cross-referencing with canvas */}
              <div style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                padding: '6px 8px',
                background: 'var(--color-surface-raised, rgba(0,0,0,0.03))',
                borderBottom: '1px solid var(--color-border)',
                flexWrap: 'wrap',
              }}>
                <IndexBadge
                  index={idx + 1}
                  bgColor={pinColor}
                />
                <span style={{ fontSize: 12, opacity: 0.7 }}>
                  {PIN_SHAPE_ICON[p.shape]} {t(`adminTour.pinShape.${p.shape}`)}
                </span>
                {p.shape === 'arrow' && (
                  <span style={{ fontSize: 11, opacity: 0.65, fontVariantNumeric: 'tabular-nums' }}>
                    {Math.round(normalizeRotationDeg(p.rotation_deg))}°
                  </span>
                )}
                <span style={{
                  fontSize: 12,
                  flex: 1,
                  fontStyle: labelText ? 'normal' : 'italic',
                  opacity: labelText ? 1 : 0.45,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}>
                  {labelText || t('adminTour.untitledItem')}
                </span>
                <span style={{ fontSize: 11, opacity: 0.55, fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}>
                  ({Math.round(p.x)}, {Math.round(p.y)})
                </span>
                <LayerControls
                  t={t}
                  isTop={(layerPos.get(`pin:${p.id}`) ?? 0) === layerCount - 1}
                  isBottom={(layerPos.get(`pin:${p.id}`) ?? 0) === 0}
                  onMove={(m) => moveAnnotation('pin', p.id, m)}
                />
                <button type="button" onClick={() => onCopyPinToNext(p)} disabled={!hasNext} style={{ fontSize: 11 }}>
                  {t('adminTour.copyToNextStep')}
                </button>
                <button type="button" onClick={() => removePin(p.id)} style={{ fontSize: 11 }}>
                  {t('adminTour.delete')}
                </button>
              </div>

              {/* Card body — editable fields */}
              <div style={{ padding: '8px 8px 8px', display: 'grid', gap: 6 }}>
                {p.shape === 'arrow' && (
                  <div style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
                    <button type="button" onClick={() => updatePin(p.id, { rotation_deg: normalizeRotationDeg(p.rotation_deg - 45) })} title={t('adminTour.rotateLeft')}>−45°</button>
                    <span style={{ minWidth: 34, textAlign: 'center', fontVariantNumeric: 'tabular-nums' }}>{Math.round(normalizeRotationDeg(p.rotation_deg))}°</span>
                    <button type="button" onClick={() => updatePin(p.id, { rotation_deg: normalizeRotationDeg(p.rotation_deg + 45) })} title={t('adminTour.rotateRight')}>+45°</button>
                  </div>
                )}
                <ColorControls
                  t={t}
                  color={p.color}
                  names="adminTour.pinColor"
                  pickerLabel={t('adminTour.pinColorPicker')}
                  onChange={(c) => updatePin(p.id, { color: c })}
                />
                <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span style={{ flexShrink: 0 }}>{t('adminTour.pinLabel')}</span>
                  <input
                    type="text"
                    value={p.label}
                    maxLength={255}
                    placeholder={t('adminTour.pinLabelPlaceholder')}
                    onChange={(e) => updatePin(p.id, { label: e.target.value })}
                    style={{ flex: 1 }}
                  />
                </label>
              </div>
            </div>
          )
        })}
      </Section>

      {/* ── Shapes ───────────────────────────────────────────── */}
      <Section title={t('adminTour.section.shapes')}>
        {/* Add-shape toolbar */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          {SHAPE_KINDS.map((kind) => {
            const active = addingShape === kind
            return (
              <button
                key={kind}
                type="button"
                onClick={() => toggleAddingShape(kind)}
                aria-pressed={active}
                style={{
                  fontWeight: active ? 700 : undefined,
                  outline: active ? '2px solid var(--color-brand-600)' : undefined,
                }}
              >
                {SHAPE_KIND_ICON[kind]} {active ? t('adminTour.cancel') : t(`adminTour.addShape.${kind}`)}
              </button>
            )
          })}
          {addingShape && (
            <span style={{ fontSize: 12, opacity: 0.75 }}>
              {addingShape === 'freehand'
                ? t('adminTour.dragCanvasToTrace')
                : addingShape === 'circle'
                ? t('adminTour.dragCanvasToDrawCircle')
                : t('adminTour.dragCanvasToDraw')}
            </span>
          )}
        </div>

        {(editState.shapes_l0 || []).length === 0 && (
          <div style={{ fontSize: 12, opacity: 0.6, marginTop: 6 }}>{t('adminTour.noShapes')}</div>
        )}

        {(editState.shapes_l0 || []).map((sh, idx) => {
          const kind = normalizeShapeKind(sh.kind)
          const strokeColor = resolveShapeColor(sh.stroke_color)
          const labelText = (sh.label || '').trim()
          return (
            <div
              key={sh.id}
              style={{
                border: '1px solid var(--color-border)',
                borderLeft: `4px solid ${strokeColor}`,
                borderRadius: 8,
                marginTop: 8,
                overflow: 'hidden',
              }}
            >
              {/* Card header */}
              <div style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                padding: '6px 8px',
                background: 'var(--color-surface-raised, rgba(0,0,0,0.03))',
                borderBottom: '1px solid var(--color-border)',
                flexWrap: 'wrap',
              }}>
                <IndexBadge
                  index={idx + 1}
                  bgColor={strokeColor}
                />
                <span style={{ fontSize: 12, opacity: 0.7 }}>
                  {SHAPE_KIND_ICON[kind]} {t(`adminTour.shapeKind.${kind}`)}
                </span>
                <span style={{
                  fontSize: 12,
                  flex: 1,
                  fontStyle: labelText ? 'normal' : 'italic',
                  opacity: labelText ? 1 : 0.45,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}>
                  {labelText || t('adminTour.untitledItem')}
                </span>
                <span style={{ fontSize: 11, opacity: 0.55, fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}>
                  {Math.round(sh.w)}×{Math.round(sh.h)}
                </span>
                <LayerControls
                  t={t}
                  isTop={(layerPos.get(`shape:${sh.id}`) ?? 0) === layerCount - 1}
                  isBottom={(layerPos.get(`shape:${sh.id}`) ?? 0) === 0}
                  onMove={(m) => moveAnnotation('shape', sh.id, m)}
                />
                <button type="button" onClick={() => onCopyShapeToNext(sh)} disabled={!hasNext} style={{ fontSize: 11 }}>
                  {t('adminTour.copyToNextStep')}
                </button>
                <button type="button" onClick={() => removeShape(sh.id)} style={{ fontSize: 11 }}>
                  {t('adminTour.delete')}
                </button>
              </div>

              {/* Card body */}
              <div style={{ padding: '8px 8px 8px', display: 'grid', gap: 6 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, flexWrap: 'wrap' }}>
                  <span style={{ opacity: 0.65 }}>({Math.round(sh.x)}, {Math.round(sh.y)})</span>
                  <select
                    value={normalizeShapeLabelPosition(sh.label_position)}
                    onChange={(e) => updateShape(sh.id, { label_position: e.target.value as ShapeLabelPosition })}
                    style={{ fontSize: 12 }}
                  >
                    {SHAPE_LABEL_POSITIONS.map((pos) => (
                      <option key={pos} value={pos}>{t(`adminTour.shapeLabelPosition.${pos}`)}</option>
                    ))}
                  </select>
                </div>
                <ShapeStyleControls
                  t={t}
                  style={{
                    stroke_color: sh.stroke_color,
                    fill_enabled: sh.fill_enabled,
                    fill_opacity: sh.fill_opacity,
                  }}
                  onChange={(patch) => updateShape(sh.id, patch)}
                />
                {kind === 'freehand' && (
                  <label style={{ fontSize: 12, display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                    <input
                      type="checkbox"
                      checked={sh.smoothing !== false}
                      onChange={(e) => updateShape(sh.id, { smoothing: e.target.checked })}
                    />
                    <span>{t('adminTour.shapeSmoothing')}</span>
                  </label>
                )}
                <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span style={{ flexShrink: 0 }}>{t('adminTour.shapeLabel')}</span>
                  <input
                    type="text"
                    value={sh.label || ''}
                    maxLength={255}
                    placeholder={t('adminTour.shapeLabelPlaceholder')}
                    onChange={(e) => updateShape(sh.id, { label: e.target.value })}
                    style={{ flex: 1 }}
                  />
                </label>
              </div>
            </div>
          )
        })}
      </Section>

      {/* ── Description (Markdown) ───────────────────────────── */}
      <Section title={t('adminTour.section.description')}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <div>
            <div style={{ fontSize: 11, fontWeight: 600, opacity: 0.65, marginBottom: 4 }}>{t('adminTour.markdownEditor')}</div>
            <textarea
              value={editState.description_md}
              maxLength={STEP_DESCRIPTION_MAX_LEN}
              onChange={(e) => setEditState({ ...editState, description_md: e.target.value })}
              style={{ width: '100%', minHeight: 170, fontFamily: 'monospace' }}
            />
          </div>
          <div>
            <div style={{ fontSize: 11, fontWeight: 600, opacity: 0.65, marginBottom: 4 }}>{t('adminTour.previewSanitized')}</div>
            <div style={{ border: '1px solid var(--color-border)', borderRadius: 8, minHeight: 170, padding: 8, overflow: 'auto' }}>
              <SafeMarkdown markdown={editState.description_md} adminImages />
            </div>
          </div>
        </div>
      </Section>

      {/* Next to Save, where the user's attention is when the refusal lands. */}
      {conflict && (
        <ConflictBanner
          message={t('adminTour.stepConflict')}
          reloadLabel={t('adminTour.conflictReload')}
          overwriteLabel={t('adminTour.conflictOverwrite')}
          onReload={onReloadConflict}
          onOverwrite={() => { focusLabelIfMissing(); onOverwriteConflict() }}
          disabled={saving}
        />
      )}
      <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
        <button onClick={() => { focusLabelIfMissing(); onSave() }} disabled={saving} aria-busy={saving}>{saving ? t('adminTour.saving') : t('adminTour.save')}</button>
        <button onClick={onCancel}>{t('adminTour.cancel')}</button>
      </div>
    </div>
  )
}

// Preset swatches, a native picker and a hex text field for one annotation
// color (a pin's, or a shape's stroke). A preset is stored by name, a custom
// color as "#rrggbb"; the text field commits only a valid hex, on blur.
// `names` is the i18n key prefix of the preset names (`${names}.${preset}`).
function ColorControls({
  t,
  color,
  names,
  pickerLabel,
  onChange,
  children,
}: {
  t: (key: any, vars?: any) => string
  color: string
  names: string
  pickerLabel: string
  onChange: (next: string) => void
  children?: React.ReactNode
}) {
  const [hexDraft, setHexDraft] = React.useState<string>('')
  React.useEffect(() => {
    setHexDraft(Object.prototype.hasOwnProperty.call(PIN_COLORS, color)
      ? PIN_COLORS[color as keyof typeof PIN_COLORS]
      : color || '')
  }, [color])

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      {children}
      {PIN_COLOR_ORDER.map((name) => {
        const selected = color === name
        return (
          <button
            key={name}
            type="button"
            onClick={() => onChange(name)}
            title={t(`${names}.${name}`)}
            aria-label={t(`${names}.${name}`)}
            aria-pressed={selected}
            style={{
              width: 20,
              height: 20,
              borderRadius: '50%',
              background: PIN_COLORS[name],
              border: selected ? '2px solid var(--color-text, #111)' : '1px solid rgba(0,0,0,0.25)',
              boxShadow: selected ? '0 0 0 2px rgba(255,255,255,0.9) inset' : 'none',
              cursor: 'pointer',
              padding: 0,
            }}
          />
        )
      })}
      <input
        type="color"
        value={isHexColor(hexDraft) ? hexDraft : resolvePinColor(color)}
        onChange={(e) => {
          const v = e.target.value
          setHexDraft(v)
          onChange(v)
        }}
        aria-label={pickerLabel}
        title={pickerLabel}
        style={{ width: 28, height: 24, border: '1px solid var(--color-border)', borderRadius: 4, padding: 0, background: 'transparent' }}
      />
      <input
        type="text"
        value={hexDraft}
        maxLength={32}
        placeholder="#rrggbb"
        onChange={(e) => setHexDraft(e.target.value)}
        onBlur={() => {
          if (isHexColor(hexDraft)) onChange(hexDraft.trim())
        }}
        style={{ width: 96, fontSize: 12, fontFamily: 'monospace' }}
      />
    </div>
  )
}

// Reusable color / fill / opacity controls used on each shape's edit card.
function ShapeStyleControls({
  t,
  style,
  onChange,
}: {
  t: (key: any, vars?: any) => string
  style: { stroke_color: string; fill_enabled: boolean; fill_opacity: number }
  onChange: (patch: { stroke_color?: string; fill_enabled?: boolean; fill_opacity?: number }) => void
}) {
  return (
    <div style={{
      border: '1px dashed var(--color-border)',
      borderRadius: 8,
      padding: 8,
      display: 'grid',
      gap: 6,
    }}>
      <ColorControls
        t={t}
        color={style.stroke_color}
        names="adminTour.shapeColor"
        pickerLabel={t('adminTour.shapeColorPicker')}
        onChange={(c) => onChange({ stroke_color: c })}
      >
        <span style={{ fontSize: 12, opacity: 0.85 }}>{t('adminTour.shapeStrokeColor')}</span>
      </ColorControls>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
          <input
            type="checkbox"
            checked={style.fill_enabled}
            onChange={(e) => onChange({ fill_enabled: e.target.checked })}
          />
          {t('adminTour.shapeFillEnabled')}
        </label>
        <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, opacity: style.fill_enabled ? 1 : 0.5 }}>
          <span>{t('adminTour.shapeFillOpacity')}</span>
          <input
            type="range"
            min={0}
            max={100}
            step={1}
            value={Math.round(style.fill_opacity * 100)}
            disabled={!style.fill_enabled}
            onChange={(e) => onChange({ fill_opacity: Number(e.target.value) / 100 })}
            style={{ width: 120 }}
          />
          <span style={{ minWidth: 34, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
            {Math.round(style.fill_opacity * 100)}%
          </span>
        </label>
      </div>
    </div>
  )
}
