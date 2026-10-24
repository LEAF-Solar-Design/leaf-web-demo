import { useReducer } from 'react'
import { FORMATS, PRESETS, RENDERER_VERSION, shapeFor } from './widgetCatalog.js'
import { DESIGNER_DEFAULT_DOC } from './designerDefault.js'

export const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w']
export const HEIGHT_BOUNDS = { la: [60, 220], diExp: [84, 200] }
export const CUTOUTS = { diExp: { x: 122.5, y: 0, w: 126, h: 37 }, diCompact: { x: 62, y: 0, w: 126, h: 37 } }
export const supportsPreset = (format, preset) => format !== 'lockInline' || (shapeFor(preset) === 'text' && preset !== 'streak')
const clamp = (n, min, max) => Math.max(min, Math.min(max, n))
export const defaultSurface = (id) => ({ h: FORMATS[id].h, radius: FORMATS[id].radius, bg: FORMATS[id].bg, vibrant: FORMATS[id].accessory, ship: true, els: [] })
export function createWidgetDoc(seed) {
  return seed ? JSON.parse(JSON.stringify(seed)) : { schema: 'widget-doc.v1', min_renderer: RENDERER_VERSION, active: 'la', formats: Object.fromEntries(Object.keys(FORMATS).map((id) => [id, defaultSurface(id)])) }
}
export function createEditorState(doc = createWidgetDoc()) {
  return { doc, selectedId: null, past: [], future: [], guides: {}, transaction: null, nudgeAt: null }
}
export function clampElement(element, width, height) {
  const w = clamp(element.w, 0.01, width), h = clamp(element.h, 0.01, height)
  return { ...element, w, h, x: clamp(element.x, 0, width - w), y: clamp(element.y, 0, height - h) }
}
export function snapTargets(doc, skip) {
  const id = doc.active, f = doc.formats[id], width = FORMATS[id].w, margin = width > 200 ? 14 : 6
  const xs = [0, margin, width / 2, width - margin, width], ys = [0, f.h / 2, f.h]
  const cutout = CUTOUTS[id]
  if (cutout) { xs.push(cutout.x, cutout.x + cutout.w); ys.push(cutout.y + cutout.h) }
  for (const e of f.els) if (e.id !== skip && e.visible) { xs.push(e.x, e.x + e.w / 2, e.x + e.w); ys.push(e.y, e.y + e.h / 2, e.y + e.h) }
  return { xs, ys }
}
export function snap1(values, targets) {
  let best = null
  for (const target of targets) for (const value of values) {
    const delta = target - value
    if (Math.abs(delta) <= 4 && (!best || Math.abs(delta) < Math.abs(best.delta))) best = { delta, target }
  }
  return best
}
// Pure pointer path. Deltas are always measured from the gesture's original box.
export function transformElement(doc, original, handle, dx, dy, alt = false) {
  const W = FORMATS[doc.active].w, H = doc.formats[doc.active].h
  const minH = original.preset === 'divider' ? 1 : 6
  const targets = snapTargets(doc, original.id), guides = {}
  let { x, y, w, h } = original
  if (handle === 'move') {
    x += dx; y += dy
    if (!alt) {
      const sx = snap1([x, x + w / 2, x + w], targets.xs), sy = snap1([y, y + h / 2, y + h], targets.ys)
      if (sx) { x += sx.delta; guides.x = sx.target }
      if (sy) { y += sy.delta; guides.y = sy.target }
    }
    x = clamp(Math.round(x), 0, W - w); y = clamp(Math.round(y), 0, H - h)
  } else {
    let left = x, right = x + w, top = y, bottom = y + h
    if (handle.includes('w')) left += dx
    if (handle.includes('e')) right += dx
    if (handle.includes('n')) top += dy
    if (handle.includes('s')) bottom += dy
    if (!alt) {
      if (/[we]/.test(handle)) {
        const west = handle.includes('w'), s = snap1([west ? left : right], targets.xs)
        if (s) { if (west) left += s.delta; else right += s.delta; guides.x = s.target }
      }
      if (/[ns]/.test(handle)) {
        const north = handle.includes('n'), s = snap1([north ? top : bottom], targets.ys)
        if (s) { if (north) top += s.delta; else bottom += s.delta; guides.y = s.target }
      }
    }
    if (handle.includes('w')) left = clamp(Math.round(left), 0, right - Math.min(6, right))
    if (handle.includes('e')) right = clamp(Math.round(right), left + Math.min(6, W - left), W)
    if (handle.includes('n')) top = clamp(Math.round(top), 0, bottom - Math.min(minH, bottom))
    if (handle.includes('s')) bottom = clamp(Math.round(bottom), top + Math.min(minH, H - top), H)
    x = left; y = top; w = right - left; h = bottom - top
  }
  // Do not draw a guide for an edge displaced by the surface clamp.
  if (![x, x + w / 2, x + w].some((v) => Math.abs(v - guides.x) < 0.51)) delete guides.x
  if (![y, y + h / 2, y + h].some((v) => Math.abs(v - guides.y) < 0.51)) delete guides.y
  return { element: { ...original, x, y, w, h }, guides }
}
function nextId(els, kind) { let n = 1; while (els.some((e) => e.id === kind + n)) n += 1; return kind + n }
export function newElement(doc, preset = 'text') {
  const d = PRESETS[preset], surface = doc.formats[doc.active], W = FORMATS[doc.active].w
  const w = Math.min(d.w, W - 8), h = Math.min(d.h, surface.h - 8)
  const e = { id: nextId(surface.els, preset), kind: shapeFor(preset), preset, min_renderer: RENDERER_VERSION,
    x: Math.round((W - w) / 2), y: Math.round((surface.h - h) / 2), w, h, scale: 1, weight: d.weight ?? 500,
    color: d.color ?? 'slate', align: d.align ?? 'left', visible: true }
  for (const key of ['arrows', 'showPct', 'count', 'tracking', 'opacity']) if (d[key] !== undefined) e[key] = d[key]
  if (preset === 'text') e.text = 'Your text'
  if (d.col) {
    e.maxRows = 3
    const columns = surface.els.filter((column) => column.visible && PRESETS[column.preset]?.col)
    if (columns.length) {
      e.y = columns[0].y; e.h = columns[0].h; e.maxRows = columns[0].maxRows
      e.x = clamp(Math.max(...columns.map((column) => column.x + column.w)) + 6, 0, W - w)
    }
  }
  if (!['brand', 'leafIcon', 'text', 'divider'].includes(preset)) {
    const field = preset === 'ring' ? 'winRate' : preset
    e.binding = { source: 'kashbot.board', field, min_renderer: RENDERER_VERSION, scale: preset === 'ring' ? 'fraction' : 'identity', tone: e.color }
    if (field === 'winRate') e.binding.window = 'today-utc'
  }
  return clampElement(e, W, surface.h)
}
export const sameDocument = (a, b) => a === b || JSON.stringify(a) === JSON.stringify(b)
function commit(state, doc, extra = {}, group = false) {
  if (sameDocument(doc, state.doc)) return state
  return { ...state, doc, past: group ? state.past : [...state.past, state.doc].slice(-150), future: [], guides: {}, nudgeAt: null, ...extra }
}
export function widgetEditorReducer(state, action) {
  if (state.transaction && !['move', 'resize', 'end', 'cancel'].includes(action.type)) return state
  const { doc } = state, id = doc.active, surface = doc.formats[id], W = FORMATS[id].w
  const selectedId = action.id ?? state.selectedId, selected = surface.els.find((e) => e.id === selectedId)
  const withSurface = (f) => ({ ...doc, formats: { ...doc.formats, [id]: f } })
  const withElements = (els) => withSurface({ ...surface, els })
  switch (action.type) {
    case 'load': return createEditorState(action.doc)
    case 'select': return { ...state, selectedId: action.id, nudgeAt: null }
    case 'format': {
      if (!Object.hasOwn(FORMATS, action.format) || action.format === id) return state
      // The document contract permits a subset of the catalog's formats.
      const formats = Object.hasOwn(doc.formats, action.format) ? doc.formats : { ...doc.formats, [action.format]: defaultSurface(action.format) }
      return commit(state, { ...doc, active: action.format, formats }, { selectedId: null, transaction: null })
    }
    case 'undo': {
      if (!state.past.length) return state
      return { ...state, doc: state.past.at(-1), past: state.past.slice(0, -1), future: [doc, ...state.future], selectedId: null, transaction: null, guides: {}, nudgeAt: null }
    }
    case 'redo': {
      if (!state.future.length) return state
      return { ...state, doc: state.future[0], past: [...state.past, doc].slice(-150), future: state.future.slice(1), selectedId: null, transaction: null, guides: {}, nudgeAt: null }
    }
    case 'begin': return { ...state, selectedId, transaction: doc, gestureSnapshot: state, nudgeAt: null }
    case 'cancel': return state.gestureSnapshot ?? state
    case 'end': return state.transaction && !sameDocument(state.transaction, doc)
      ? { ...state, past: [...state.past, state.transaction].slice(-150), future: [], transaction: null, gestureSnapshot: null, guides: {} }
      : { ...state, doc: state.transaction ?? doc, transaction: null, gestureSnapshot: null, guides: {} }
    case 'add': {
      if (surface.els.length >= 32 || !Object.hasOwn(PRESETS, action.preset ?? 'text') || !supportsPreset(id, action.preset ?? 'text')) return state
      const e = newElement(doc, action.preset)
      return commit(state, withElements([...surface.els, e]), { selectedId: e.id })
    }
    case 'duplicate': {
      if (!selected || surface.els.length >= 32) return state
      const e = clampElement({ ...selected, id: nextId(surface.els, selected.preset ?? selected.kind), x: selected.x + 8, y: selected.y + 8 }, W, surface.h)
      return commit(state, withElements([...surface.els, e]), { selectedId: e.id })
    }
    case 'delete': return selected ? commit(state, withElements(surface.els.filter((e) => e.id !== selectedId)), { selectedId: null }) : state
    case 'visibility': return selected ? commit(state, withElements(surface.els.map((e) => e.id === selectedId ? { ...e, visible: !e.visible } : e)), { selectedId: selected.visible && state.selectedId === selectedId ? null : state.selectedId }) : state
    case 'reorder': {
      const from = surface.els.findIndex((e) => e.id === selectedId)
      if (from < 0) return state
      const els = [...surface.els], [e] = els.splice(from, 1)
      els.splice(clamp(action.index, 0, els.length), 0, e)
      return commit(state, withElements(els))
    }
    case 'edit': {
      if (!selected) return state
      const element = { ...selected, ...action.patch }
      if (Object.hasOwn(action.patch, 'w')) element.w = clamp(element.w, Math.min(6, W - element.x), W - element.x)
      if (Object.hasOwn(action.patch, 'h')) element.h = clamp(element.h, Math.min(element.preset === 'divider' ? 1 : 6, surface.h - element.y), surface.h - element.y)
      if (Object.hasOwn(action.patch, 'x')) element.x = clamp(element.x, 0, W - element.w)
      if (Object.hasOwn(action.patch, 'y')) element.y = clamp(element.y, 0, surface.h - element.h)
      return commit(state, withElements(surface.els.map((e) => e.id === selectedId ? element : e)), { selectedId: !element.visible && state.selectedId === selectedId ? null : state.selectedId })
    }
    case 'move': case 'resize': {
      if (!selected) return state
      const { element, guides } = transformElement(doc, action.original ?? selected, action.type === 'move' ? 'move' : action.handle, action.dx ?? 0, action.dy ?? 0, action.alt)
      const next = withElements(surface.els.map((e) => e.id === selectedId ? element : e))
      return state.transaction ? { ...state, doc: next, guides } : commit(state, next, { guides })
    }
    case 'nudge': {
      if (!selected) return state
      const element = clampElement({ ...selected, x: selected.x + action.dx, y: selected.y + action.dy }, W, surface.h)
      const grouped = state.nudgeAt != null && action.time - state.nudgeAt < 400
      return commit(state, withElements(surface.els.map((e) => e.id === selectedId ? element : e)), { nudgeAt: action.time }, grouped)
    }
    case 'surface': {
      const f = { ...surface, ...action.patch }
      if (HEIGHT_BOUNDS[id]) f.h = clamp(f.h, ...HEIGHT_BOUNDS[id])
      f.radius = clamp(f.radius, 0, Math.floor(Math.min(W, f.h) / 2))
      if (f.h !== surface.h) f.els = f.els.map((e) => clampElement(e, W, f.h))
      return commit(state, withSurface(f))
    }
    case 'reset': return commit(state, withSurface(createWidgetDoc(DESIGNER_DEFAULT_DOC).formats[id]), { selectedId: null })
    default: return state
  }
}
export function useWidgetEditor(doc) { return useReducer(widgetEditorReducer, doc, createEditorState) }
