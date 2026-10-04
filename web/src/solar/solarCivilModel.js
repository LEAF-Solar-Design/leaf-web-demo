import { labelOf, formatValue } from './solarReadResultModel.js'

export const CIVIL_OPERATIONS = Object.freeze([
  'frame-generate', 'frame-collision-detect', 'piling-generate', 'pile-length-range-check', 'grade-pad',
])
const FIELDS = Object.freeze({
  'frame-generate': ['boundary', 'preset', 'drawing_units'],
  'frame-collision-detect': [],
  'piling-generate': ['preset', 'pile_template'],
  'pile-length-range-check': ['preset'],
  'grade-pad': ['boundary'],
})
const HASH = /^[0-9a-f]{64}$/
const DRAWING = /^[a-z0-9][a-z0-9_-]{0,62}$/
const MODES = ['Auto', 'Manual', 'Clearance']
const plain = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
  && [Object.prototype, null].includes(Object.getPrototypeOf(v))
const exact = (v, required, optional = []) => plain(v)
  && Reflect.ownKeys(v).every((k) => typeof k === 'string' && [...required, ...optional].includes(k))
  && required.every((k) => Object.hasOwn(v, k))
const hash = (v) => typeof v === 'string' && HASH.test(v)
const count = (v) => Number.isSafeInteger(v) && v >= 0 && v <= 1_000_000_000
const finite = (v) => typeof v === 'number' && Number.isFinite(v)
const coordinate = (v) => finite(v) && Math.abs(v) <= 1e9
const project = (v) => typeof v === 'string' && [...v].length >= 1 && [...v].length <= 100
const name = (v) => typeof v === 'string' && [...v].length <= 256
const copy = (v) => JSON.parse(JSON.stringify(v))
const bad = (code) => ({ ok: false, code })

// Reject values JSON.stringify would otherwise silently coerce or drop.
function jsonValue(v, seen = new Set(), depth = 0) {
  if (v === null || typeof v === 'string' || typeof v === 'boolean') return true
  if (typeof v === 'number') return Number.isFinite(v)
  if ((!Array.isArray(v) && !plain(v)) || seen.has(v)) return false
  if (Array.isArray(v) && v.length > 1_000_000) return false
  seen.add(v)
  const keys = Reflect.ownKeys(v)
  const valid = Array.isArray(v)
    ? keys.every((k) => k === 'length' || (typeof k === 'string' && /^(0|[1-9][0-9]*)$/.test(k)))
      && Array.from({ length: v.length }, (_, i) => Object.hasOwn(v, i) && jsonValue(v[i], seen, depth + 1)).every(Boolean)
    : keys.every((k) => typeof k === 'string' && jsonValue(v[k], seen, depth + 1))
  seen.delete(v)
  return valid
}

function objectValid(v, preset) {
  if (!plain(v) || !jsonValue(v)) return false
  const text = JSON.stringify(v).replace(/[\u007f-\uffff]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`)
  if (text.length > 1_000_000) return false
  for (const key of preset ? ['Name', 'PileTemplateName'] : ['Name']) {
    const matches = Object.keys(v).filter((k) => k.toLowerCase() === key.toLowerCase())
    if (matches.length > 1 || matches.some((k) => !name(v[k]))) return false
  }
  return !preset || (Object.hasOwn(v, 'Name') && name(v.Name) && v.Name.trim().length > 0)
}

function boundaryValid(v) {
  return Array.isArray(v) && v.length >= 3 && v.length <= 20_000
    && Array.from({ length: v.length }, (_, i) => {
      const p = v[i]
      return Object.hasOwn(v, i) && Array.isArray(p) && p.length === 2
        && Object.hasOwn(p, 0) && Object.hasOwn(p, 1) && coordinate(p[0]) && coordinate(p[1])
    }).every(Boolean)
}

export function validateCivilBody(body) {
  try {
    if (!plain(body)) return bad('TERRAIN_BODY_INVALID')
    const { operation } = body
    if (!CIVIL_OPERATIONS.includes(operation)) return bad('TERRAIN_OPERATION_INVALID')
    if (!exact(body, ['operation', ...FIELDS[operation]], ['expected_head', ...(operation === 'grade-pad' ? ['mode', 'value_du'] : [])])) {
      return bad('TERRAIN_BODY_INVALID')
    }
    if (!Object.hasOwn(body, 'expected_head') || (body.expected_head !== null && !hash(body.expected_head))) {
      return bad('TERRAIN_EXPECTED_HEAD_INVALID')
    }
    if (Object.hasOwn(body, 'boundary') && !boundaryValid(body.boundary)) return bad('FRAMES_PILES_BOUNDARY_INVALID')
    if (Object.hasOwn(body, 'preset') && !objectValid(body.preset, true)) return bad('FRAMES_PILES_PRESET_INVALID')
    if (Object.hasOwn(body, 'pile_template') && !objectValid(body.pile_template, false)) return bad('FRAMES_PILES_PILE_TEMPLATE_INVALID')
    if (operation === 'frame-generate' && !['m', 'ft'].includes(body.drawing_units)) return bad('FRAMES_PILES_DRAWING_UNITS_INVALID')
    if (operation === 'grade-pad') {
      const mode = Object.hasOwn(body, 'mode') ? body.mode : 'Auto'
      const value = Object.hasOwn(body, 'value_du') ? body.value_du : null
      if (!MODES.includes(mode) || (value !== null && (!coordinate(value) || mode === 'Auto' || (mode === 'Clearance' && value < 0)))) {
        return bad('CIVIL_GRADE_INPUT_INVALID')
      }
    }
    return { ok: true, body: copy(body) }
  } catch { return bad('TERRAIN_BODY_INVALID') }
}

// A bounded recursive JSON parser keeps duplicate members visible before JSON.parse loses them.
function parseUnique(text) {
  let i = 0
  const ws = () => { while (/\s/.test(text[i] ?? '') && i < text.length) i++ }
  const string = () => {
    const start = i++
    while (i < text.length) {
      if (text[i] === '\\') { i += 2; continue }
      if (text[i++] === '"') return JSON.parse(text.slice(start, i))
    }
    throw new Error('json')
  }
  const value = (depth) => {
    ws()
    if (text[i] === '"') return string()
    if (text[i] === '{') {
      i++; ws()
      const seen = new Set()
      if (text[i] !== '}') for (;;) {
        ws(); if (text[i] !== '"') throw new Error('json')
        const key = string()
        const normalized = ['name', 'piletemplatename'].includes(key.toLowerCase()) ? key.toLowerCase() : key
        if (seen.has(normalized)) throw new Error('json')
        seen.add(normalized); ws()
        if (text[i++] !== ':') throw new Error('json')
        value(depth + 1); ws()
        if (text[i] !== ',') break
        i++
      }
      if (text[i++] !== '}') throw new Error('json')
      return
    }
    if (text[i] === '[') {
      i++; ws()
      if (text[i] !== ']') for (;;) {
        value(depth + 1); ws()
        if (text[i] !== ',') break
        i++
      }
      if (text[i++] !== ']') throw new Error('json')
      return
    }
    const match = text.slice(i).match(/^(?:null|true|false|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)/)
    if (!match) throw new Error('json')
    i += match[0].length
  }
  value(0); ws()
  if (i !== text.length) throw new Error('json')
  return JSON.parse(text)
}

export function parseCivilDrafts({ operation, boundary, preset, pileTemplate, drawingUnits, mode = 'Auto', value = '' } = {}) {
  if (!CIVIL_OPERATIONS.includes(operation)) return { ok: false, field: 'operation', code: 'TERRAIN_OPERATION_INVALID' }
  const fields = {}
  for (const key of FIELDS[operation]) {
    if (key === 'drawing_units') { fields[key] = drawingUnits; continue }
    const draft = key === 'boundary' ? boundary : key === 'preset' ? preset : pileTemplate
    const code = key === 'boundary' ? 'FRAMES_PILES_BOUNDARY_INVALID' : key === 'preset' ? 'FRAMES_PILES_PRESET_INVALID' : 'FRAMES_PILES_PILE_TEMPLATE_INVALID'
    try {
      if (typeof draft !== 'string' || draft.length > (key === 'boundary' ? 2_000_000 : 1_000_000)) throw new Error('json')
      fields[key] = parseUnique(draft)
    } catch { return { ok: false, field: key, code } }
  }
  if (operation === 'grade-pad') {
    fields.mode = mode
    if (typeof value !== 'string' || value.length > 64
      || (value.trim() !== '' && !/^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/.test(value.trim()))) {
      return { ok: false, field: 'value_du', code: 'CIVIL_GRADE_INPUT_INVALID' }
    }
    fields.value_du = value.trim() === '' ? null : Number(value)
  }
  const checked = validateCivilBody({ operation, expected_head: null, ...fields })
  if (!checked.ok) {
    const field = checked.code.includes('BOUNDARY') ? 'boundary' : checked.code.includes('PILE_TEMPLATE') ? 'pile_template'
      : checked.code.includes('PRESET') ? 'preset' : checked.code.includes('UNITS') ? 'drawing_units'
        : !MODES.includes(mode) ? 'mode' : 'value_du'
    return { ok: false, field, code: checked.code }
  }
  return { ok: true, fields }
}

const HEAD_KEYS = ['schema', 'drawing_id', 'project_id', 'index', 'parent', 'state']
const REF_KEYS = ['schema', 'artifact_id', 'media_type', 'filename', 'byte_length', 'content_sha256', 'source_version', 'download']
function refCopy(v, drawingId) {
  if (!exact(v, REF_KEYS) || v.schema !== 'leaf.solar-artifact-ref.v1' || !hash(v.artifact_id) || !hash(v.content_sha256)
    || v.media_type !== 'application/json' || v.filename !== 'physical-state.json'
    || !Number.isInteger(v.byte_length) || v.byte_length < 1 || v.byte_length > 16_777_216
    || !Number.isSafeInteger(v.source_version) || v.source_version < 1
    || v.download !== `/api/drawings/${drawingId}/artifacts/${v.artifact_id}`) return null
  return copy(v)
}
function headCopy(v, drawingId, projectId) {
  if (!exact(v, HEAD_KEYS) || v.schema !== 'leaf.solar-physical-head.v1' || v.drawing_id !== drawingId || !project(v.project_id)
    || (projectId !== null && v.project_id !== projectId) || !Number.isInteger(v.index) || v.index < 0 || v.index > 4095
    || (v.index === 0 ? v.parent !== null : !hash(v.parent)) || refCopy(v.state, drawingId) === null) return null
  return copy(v)
}
const envelope = (v) => (!Object.hasOwn(v, 'error') || v.error === null)
  && (!Object.hasOwn(v, 'degraded_mode') || v.degraded_mode === false)
const scopeValid = (d, p) => typeof d === 'string' && DRAWING.test(d) && (p === null || project(p))
function previewValid(v) {
  return exact(v, ['schema', 'maturity', 'frames', 'piles', 'collision_markers', 'range_markers', 'terrain'])
    && v.schema === 'leaf.solar-frames-piles-preview.v1' && v.maturity === 'preview'
    && ['frames', 'piles', 'collision_markers', 'range_markers'].every((k) => count(v[k])) && typeof v.terrain === 'boolean'
}
function standingValid(v, preview) {
  if (!exact(v, ['schema', 'maturity', 'grid_sha256', 'frames', 'piles'])
    || v.schema !== 'leaf.solar-frames-piles-terrain-standing.v1' || v.maturity !== 'preview'
    || (v.grid_sha256 !== null && !hash(v.grid_sha256)) || (preview.terrain !== (v.grid_sha256 !== null))) return false
  return ['frames', 'piles'].every((key) => {
    const e = v[key]
    return exact(e, ['state', 'checked', 'stale']) && count(e.checked) && count(e.stale)
      && e.stale <= e.checked && e.checked <= preview[key]
      && e.state === (e.checked === 0 ? 'absent' : e.stale > 0 ? 'stale' : 'current')
  })
}

export function validateCivilView(body, scope = {}) {
  try {
    if (!plain(scope)) return null
    const { drawingId, projectId = null } = scope
    if (!scopeValid(drawingId, projectId) || !exact(body, ['schema', 'stored', 'head', 'preview', 'standing', 'grade_pads'], ['error', 'degraded_mode'])
      || !envelope(body) || body.schema !== 'leaf.solar-civil-view-response.v1' || typeof body.stored !== 'boolean' || !count(body.grade_pads)) return null
    if (!body.stored) {
      if (body.head !== null || body.preview !== null || body.standing !== null || body.grade_pads !== 0) return null
    } else if (headCopy(body.head, drawingId, projectId) === null || !previewValid(body.preview) || !standingValid(body.standing, body.preview)) return null
    const result = copy(body); delete result.error; delete result.degraded_mode
    return result
  } catch { return null }
}

const SUMMARY_KEYS = {
  'frame-generate': ['frames_added', 'frames_off_terrain', 'preset_name'],
  'frame-collision-detect': ['frames_checked', 'collisions'],
  'piling-generate': ['native_frames', 'piles', 'piles_replaced', 'grid_piles', 'joint_piles', 'station_piles', 'short_trackers', 'template_name'],
  'pile-length-range-check': ['total_piles', 'out_of_range', 'min_m', 'max_m'],
  'grade-pad': ['pads_added', 'grade_pads', 'mode', 'elevation_m', 'label', 'total_cut_m3', 'total_fill_m3', 'net_m3'],
}
function summaryValid(v, op) {
  if (!exact(v, SUMMARY_KEYS[op])) return false
  if (!SUMMARY_KEYS[op].every((k) => {
    if (['preset_name', 'template_name', 'label'].includes(k)) return name(v[k])
    if (k === 'mode') return MODES.includes(v[k])
    if (['min_m', 'max_m', 'elevation_m', 'total_cut_m3', 'total_fill_m3', 'net_m3'].includes(k)) return finite(v[k])
    return count(v[k])
  })) return false
  if (op === 'frame-generate') return v.frames_off_terrain <= v.frames_added
  if (op === 'pile-length-range-check') return v.out_of_range <= v.total_piles && v.min_m <= v.max_m
  return true
}

export function validateCivilOperation(body, request) {
  try {
    if (!plain(request)) return null
    const { drawingId, projectId = null } = request
    const sent = request.body ?? { operation: request.operation, expected_head: request.expectedHead }
    const { operation, expected_head: base } = sent
    if (!scopeValid(drawingId, projectId) || !CIVIL_OPERATIONS.includes(operation) || (base !== null && !hash(base))) return null
    const keys = ['schema', 'operation', 'maturity', 'outcome', 'created', 'drawing_id', 'project_id', 'base', 'units', 'terrain', 'summary', 'preview', 'standing', 'head']
    if (!exact(body, keys, ['error', 'degraded_mode']) || !envelope(body)
      || body.schema !== (operation === 'grade-pad' ? 'leaf.solar-civil-operation.v1' : 'leaf.solar-frames-piles.v1')
      || body.operation !== operation || body.maturity !== 'preview' || !['published', 'retry', 'unchanged'].includes(body.outcome)
      || body.created !== (body.outcome === 'published') || body.drawing_id !== drawingId || !project(body.project_id)
      || (projectId !== null && body.project_id !== projectId) || body.base !== base) return null
    const head = headCopy(body.head, drawingId, body.project_id)
    if (head === null || (body.outcome === 'unchanged' ? head.state.artifact_id !== base : head.parent !== base)) return null
    if (!exact(body.units, ['drawing_units', 'meters_per_unit'])
      || !['m', 'ft'].includes(body.units.drawing_units)
      || body.units.meters_per_unit !== (body.units.drawing_units === 'm' ? 1 : 0.3048)) return null
    if (Object.hasOwn(sent, 'drawing_units') && sent.drawing_units !== body.units.drawing_units) return null
    const t = body.terrain
    if (!exact(t, ['present', 'sampled', 'rows', 'cols', 'grid_sha256']) || typeof t.present !== 'boolean' || typeof t.sampled !== 'boolean') return null
    if (t.present ? (!Number.isSafeInteger(t.rows) || t.rows < 1 || !Number.isSafeInteger(t.cols) || t.cols < 1 || !hash(t.grid_sha256))
      : (t.rows !== null || t.cols !== null || t.grid_sha256 !== null)) return null
    const sampled = operation === 'grade-pad' || (t.present && ['frame-generate', 'piling-generate'].includes(operation))
    if (t.sampled !== sampled || !previewValid(body.preview) || body.preview.terrain !== t.present
      || !standingValid(body.standing, body.preview) || body.standing.grid_sha256 !== t.grid_sha256) return null
    if (body.outcome === 'retry' ? body.summary !== null : !summaryValid(body.summary, operation)) return null
    const result = copy(body); delete result.error; delete result.degraded_mode
    return result
  } catch { return null }
}

export function civilSummary(value) {
  const rows = (v) => Object.entries(v ?? {}).map(([key, item]) => ({ key, label: labelOf(key), text: formatValue(item) }))
  return {
    counts: rows(value?.preview && Object.fromEntries(['frames', 'piles', 'collision_markers', 'range_markers'].map((k) => [k, value.preview[k]]))),
    standing: value?.standing ? ['frames', 'piles'].map((entity) => ({ entity, ...value.standing[entity] })) : [],
    outcome: value?.outcome ?? null, summary: rows(value?.summary),
  }
}
