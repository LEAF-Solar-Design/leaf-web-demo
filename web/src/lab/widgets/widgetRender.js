import { FORMATS, TONES } from './widgetCatalog.js'
import { validateWidgetDoc } from './widgetDoc.js'
import { resolveBinding } from './widgetResolve.js'

const LEAF = 'M20.5 3.5C11 3.2 4.6 8 4.3 15.2L3 21.1 4.2 22.3 6.3 20.2C16 24 23 14 20.5 3.5Z'
// The render tree contains only text, boxes and paths with numeric geometry.
export function renderWidget(doc, { format = doc.active, board = {}, renderer } = {}) {
  const validated = validateWidgetDoc(doc, renderer)
  const surface = validated.formats[format]
  if (!surface) throw new Error('Unknown format.')
  const nodes = []
  const emit = (e, patch) => nodes.push({
    id: e.id, kind: e.kind, x: e.x, y: e.y, w: e.w, h: e.h,
    scale: e.scale, weight: e.weight, align: e.align, opacity: e.opacity ?? 1,
    color: TONES[surface.vibrant ? 'ink' : e.color], fontSize: e.h * 0.8 * e.scale,
    ...patch,
  })
  for (const e of surface.els.filter((element) => element.visible)) {
    const values = e.binding ? resolveBinding(e.binding, board, e.maxRows ?? e.count ?? 3) : []
    if (e.preset === 'ring') {
      const v = values[0], r = Math.min(e.w, e.h) * 0.42, cx = e.w / 2, cy = e.h / 2
      const fraction = Math.max(0, Math.min(0.999999, v?.fraction ?? 0)), angle = fraction * Math.PI * 2
      emit(e, { path: `M ${cx} ${cy - r} A ${r} ${r} 0 ${fraction > 0.5 ? 1 : 0} 1 ${cx + Math.sin(angle) * r} ${cy - Math.cos(angle) * r}`, stroke: true, color: TONES[surface.vibrant ? 'ink' : v?.tone ?? e.color] })
      emit(e, { id: e.id + '-label', kind: 'text', text: v?.text ?? 'Not reported', align: 'center', fontSize: e.h * 0.22 * e.scale })
    } else if (e.preset === 'spark') {
      const data = values.filter((v) => v.value != null)
      const low = Math.min(0, ...data.map((v) => v.value)), high = Math.max(0, ...data.map((v) => v.value))
      emit(e, { path: data.map((v, i) => `${i ? 'L' : 'M'} ${2 + i / Math.max(1, data.length - 1) * Math.max(0, e.w - 4)} ${e.h - 2 - (v.value - low) / (high - low || 1) * Math.max(0, e.h - 4)}`).join(' '), stroke: true })
    } else if (e.preset === 'leafIcon') {
      emit(e, { path: LEAF, viewBox: '0 0 24 24' })
    } else if (e.preset === 'oddsBar') {
      if (!values.length) emit(e, { kind: 'text', text: 'Not reported', color: TONES.slate })
      values.forEach((v, i) => emit(e, { id: e.id + '-' + i, y: e.y + i * e.h / values.length, h: e.h / values.length * 0.26, w: e.w * Math.max(0, Math.min(1, v.value ?? 0)) }))
    } else if (e.kind === 'text') {
      const rows = values.length ? values : [{ text: e.preset === 'brand' ? 'KASHBOT' : e.text ?? (e.binding ? 'Not reported' : ''), tone: e.color }]
      rows.forEach((v, i) => emit(e, { id: e.id + '-' + i, y: e.y + i * e.h / rows.length, h: e.h / rows.length, fontSize: e.h / rows.length * 0.8 * e.scale, text: v.text, color: TONES[surface.vibrant ? 'ink' : v.tone] }))
    } else emit(e, e.kind === 'svg-path' ? { path: e.path ?? '' } : {})
  }
  return { schema: 'widget-render-tree.v1', format, width: FORMATS[format].w, height: surface.h, radius: surface.radius, background: surface.bg === 'none' ? null : '--leaf-color-background', nodes }
}
export const renderTree = renderWidget
