import { FORMATS, PRESETS, PRIMITIVES, BINDING_FIELDS, RENDERER_VERSION, shapeFor } from './widgetCatalog.js'

export const CANVAS_WIDTH = 361
export const CANVAS_HEIGHT = 160
export const MAX_ELEMENTS = 32
export const MAX_TEXT_LENGTH = 512

const fields = new Set(['id', 'kind', 'x', 'y', 'w', 'h', 'scale', 'weight', 'color', 'align', 'visible', 'text'])
const finite = (value) => typeof value === 'number' && Number.isFinite(value)

// A closed text-only boundary: unknown properties are refused, including executable data.
export function validateElement(element) {
  if (!element || typeof element !== 'object' || Array.isArray(element)) throw new Error('Expected a text element.')
  if (Reflect.ownKeys(element).some((key) => !fields.has(key))) throw new Error('Unsupported element field.')
  if ([...fields].some((key) => !Object.hasOwn(element, key))) throw new Error('All text element fields are required.')
  if (element.kind !== 'text') throw new Error('Only text elements are supported.')
  if (typeof element.id !== 'string' || !element.id.trim()) throw new Error('A stable element ID is required.')
  if (typeof element.text !== 'string' || element.text.length > MAX_TEXT_LENGTH) throw new Error('Text must be at most 512 characters.')
  const { x, y, w, h, scale, weight } = element
  if (![x, y, w, h].every(finite) || x < 0 || y < 0 || w <= 0 || h <= 0 || x + w > CANVAS_WIDTH || y + h > CANVAS_HEIGHT) {
    throw new Error('Geometry must be finite and inside the 361 × 160 canvas.')
  }
  if (!finite(scale) || scale < 0.4 || scale > 2) throw new Error('Scale must be between 0.4 and 2.')
  if (![400, 500, 600, 700, 800, 900].includes(weight)) throw new Error('Unsupported text weight.')
  if (!['ink', 'leaf', 'slate', 'loss'].includes(element.color)) throw new Error('Unsupported text color.')
  if (!['left', 'center', 'right'].includes(element.align)) throw new Error('Unsupported alignment.')
  if (typeof element.visible !== 'boolean') throw new Error('Visibility must be a boolean.')
  return { ...element }
}

export function validateElements(elements) {
  if (!Array.isArray(elements) || elements.length > MAX_ELEMENTS) throw new Error('A draft supports at most 32 text elements.')
  const validated = Array.from(elements, validateElement)
  if (new Set(validated.map(({ id }) => id)).size !== validated.length) throw new Error('Element IDs must be unique.')
  return validated
}

export function createTextElement(id) {
  return validateElement({ id, kind: 'text', x: 16, y: 16, w: 120, h: 16, scale: 1, weight: 500, color: 'slate', align: 'left', visible: true, text: 'Your text' })
}

export function textInputs(element) {
  return { text: element.text, x: String(element.x), y: String(element.y), align: element.align }
}

// Inputs stay strings until validated, so blank/invalid numeric edits survive in the editor.
export function validateTextInputs(element, input) {
  const errors = {}
  if (typeof input.text !== 'string' || input.text.length > MAX_TEXT_LENGTH) errors.text = 'Text must be at most 512 characters.'
  for (const [axis, maximum] of [['x', CANVAS_WIDTH - element.w], ['y', CANVAS_HEIGHT - element.h]]) {
    const raw = input[axis]
    if (typeof raw !== 'string' || !/^[+-]?(?:\d+\.?\d*|\.\d+)$/.test(raw.trim()) || !Number.isFinite(Number(raw)) || Number(raw) < 0 || Number(raw) > maximum) {
      errors[axis] = `${axis.toUpperCase()} must be a finite number from 0 to ${maximum}.`
    }
  }
  if (!['left', 'center', 'right'].includes(input.align)) errors.align = 'Choose left, center or right alignment.'
  if (Object.keys(errors).length) return { errors, element: null }
  return { errors, element: validateElement({ ...element, ...input, x: Number(input.x), y: Number(input.y) }) }
}

const closed = (value, keys) => {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Reflect.ownKeys(value).some((key) => !keys.includes(key))) throw new Error('Unsupported document field.')
}
const bounded = (value, min, max) => {
  if (!finite(value) || value < min || value > max) throw new Error('Numeric value outside bounds.')
}
export function requireRenderer(minimum, renderer = RENDERER_VERSION) {
  const parse = (v) => {
    if (typeof v !== 'string' || !/^\d{1,4}\.\d{1,4}\.\d{1,4}$/.test(v)) throw new Error('Invalid renderer version.')
    return v.split('.').map(Number)
  }
  const need = parse(minimum), have = parse(renderer)
  for (let i = 0; i < 3; i += 1) {
    if (have[i] > need[i]) return
    if (have[i] < need[i]) throw new Error('Unsupported min_renderer.')
  }
}
const elementKeys = ['id', 'kind', 'x', 'y', 'w', 'h', 'scale', 'weight', 'color', 'align', 'visible', 'text', 'preset', 'binding', 'min_renderer', 'arrows', 'count', 'maxRows', 'tracking', 'showPct', 'opacity', 'path']
export function validateWidgetDoc(doc, renderer = RENDERER_VERSION) {
  requireRenderer(RENDERER_VERSION, renderer)
  closed(doc, ['schema', 'min_renderer', 'active', 'formats'])
  if (doc.schema !== 'widget-doc.v1') throw new Error('Unsupported widget schema.')
  requireRenderer(doc.min_renderer, renderer)
  closed(doc.formats, Object.keys(FORMATS))
  if (!Object.keys(doc.formats).length || typeof doc.active !== 'string' || !Object.hasOwn(doc.formats, doc.active)) throw new Error('Unknown active format.')
  for (const [id, format] of Object.entries(doc.formats)) {
    closed(format, ['h', 'radius', 'bg', 'vibrant', 'ship', 'els'])
    bounded(format.h, 1, 1024); bounded(format.radius, 0, 512)
    if (!['black', 'blur', 'none'].includes(format.bg) || typeof format.vibrant !== 'boolean' || typeof format.ship !== 'boolean') throw new Error('Invalid surface.')
    if (!Array.isArray(format.els) || format.els.length > MAX_ELEMENTS) throw new Error('At most 32 elements per format.')
    const ids = new Set()
    for (const e of format.els) {
      closed(e, elementKeys)
      if (typeof e.id !== 'string' || !/^[a-zA-Z0-9_-]{1,64}$/.test(e.id) || ids.has(e.id)) throw new Error('Invalid or duplicate element ID.')
      ids.add(e.id)
      if (!PRIMITIVES.includes(e.kind)) throw new Error('Unknown primitive kind.')
      requireRenderer(e.min_renderer, renderer)
      if (e.preset !== undefined && (!Object.hasOwn(PRESETS, e.preset) || shapeFor(e.preset) !== e.kind)) throw new Error('Unknown preset or shape.')
      bounded(e.x, 0, FORMATS[id].w); bounded(e.y, 0, format.h)
      bounded(e.w, 0.01, FORMATS[id].w); bounded(e.h, 0.01, format.h)
      if (e.x + e.w > FORMATS[id].w || e.y + e.h > format.h) throw new Error('Geometry outside surface.')
      bounded(e.scale, 0.4, 2); bounded(e.weight, 400, 900)
      if (e.weight % 100 || !['auto', 'ink', 'leaf', 'slate', 'loss'].includes(e.color) || !['left', 'center', 'right'].includes(e.align) || typeof e.visible !== 'boolean') throw new Error('Invalid primitive style.')
      for (const key of ['arrows', 'showPct']) if (e[key] !== undefined && typeof e[key] !== 'boolean') throw new Error('Invalid boolean.')
      for (const [key, min, max] of [['count', 1, 10], ['maxRows', 1, 3], ['tracking', 0, 20], ['opacity', 0, 1]]) if (e[key] !== undefined) {
        bounded(e[key], min, max)
        if (['count', 'maxRows'].includes(key) && !Number.isInteger(e[key])) throw new Error('Integer required.')
      }
      if (['history', 'historyList'].includes(e.preset) && e.count > 4) throw new Error('Ledger supports at most 4 rows.')
      if (e.text !== undefined && (e.kind !== 'text' || typeof e.text !== 'string' || e.text.length > 512)) throw new Error('Invalid text.')
      if (e.path !== undefined && (e.kind !== 'svg-path' || typeof e.path !== 'string' || e.path.length > 8192 || !/^[MmLlHhVvCcSsQqTtAaZz0-9., +\-]+$/.test(e.path))) throw new Error('Invalid SVG path.')
      if (e.binding !== undefined) {
        closed(e.binding, ['source', 'field', 'min_renderer', 'scale', 'tone', 'window'])
        const b = e.binding
        requireRenderer(b.min_renderer, renderer)
        if (b.source !== 'kashbot.board' || !BINDING_FIELDS.includes(b.field)) throw new Error('Unknown binding.')
        if (b.field === 'winRate' && b.window !== 'today-utc') throw new Error('Win rate requires the today-utc window field.')
        if (b.window !== undefined && b.window !== 'today-utc') throw new Error('Unknown window field.')
        if (!['fraction', 'today-percent', 'open-count', 'identity'].includes(b.scale) || !['auto', 'ink', 'leaf', 'slate', 'loss'].includes(b.tone)) throw new Error('Invalid binding scale or tone.')
      }
    }
  }
  return JSON.parse(JSON.stringify(doc))
}

export function migrate(input) {
  if (input?.schema === 'widget-doc.v1') return validateWidgetDoc(input)
  closed(input, ['v', 'active', 'formats'])
  if (input.v !== 2) throw new Error('Only designer v2 can migrate.')
  closed(input.formats, Object.keys(FORMATS))
  const formats = {}
  for (const [id, f] of Object.entries(input.formats)) {
    closed(f, ['h', 'radius', 'bg', 'vibrant', 'ship', 'els'])
    if (!Array.isArray(f.els) || f.els.length > MAX_ELEMENTS) throw new Error('At most 32 elements per format.')
    formats[id] = { ...f, els: f.els.map((e) => {
      closed(e, [...elementKeys.filter((key) => !['preset', 'binding', 'min_renderer', 'path'].includes(key)), 'source'])
      if (!Object.hasOwn(PRESETS, e.kind)) throw new Error('Unknown designer kind.')
      const { kind, source, ...rest } = e
      if (source !== undefined && kind !== 'ring') throw new Error('Unexpected source.')
      const result = { ...rest, kind: shapeFor(kind), preset: kind, min_renderer: RENDERER_VERSION }
      if (!['brand', 'leafIcon', 'text', 'divider'].includes(kind)) {
        const field = kind === 'ring' ? source ?? 'winRate' : kind
        result.binding = { source: 'kashbot.board', field, min_renderer: RENDERER_VERSION, scale: kind === 'ring' ? field === 'todayPct' ? 'today-percent' : field === 'openCount' ? 'open-count' : 'fraction' : 'identity', tone: e.color }
        if (field === 'winRate') result.binding.window = 'today-utc'
      } else if (source !== undefined) throw new Error('Unexpected source.')
      return result
    }) }
  }
  return validateWidgetDoc({ schema: 'widget-doc.v1', min_renderer: RENDERER_VERSION, active: input.active, formats })
}
