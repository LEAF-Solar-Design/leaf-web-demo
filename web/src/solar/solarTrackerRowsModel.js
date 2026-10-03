// Pure tracker row request and result contracts. No transport or persisted-state eligibility.
export const TRACKER_ROWS_OPERATION = 'manual-create'
export const TRACKER_ROWS_RESULT_SCHEMA = 'leaf.solar-tracker-rows.v1'
export const TRACKER_ROWS_MAX_ROWS = 256
export const TRACKER_ROWS_MAX_ROW_SLOTS = 10_000
export const TRACKER_ROWS_MAX_TOTAL_SLOTS = 100_000
export const TRACKER_ROWS_MAX_REQUEST_BYTES = 262_144

const BODY_KEYS = ['operation', 'rows', 'module_power_watts', 'expected_head']
const DRAFT_KEYS = ['rows', 'modulePowerWatts', 'expectedHead', 'drawingUnits']
const ROW_KEYS = ['axis_start', 'axis_end', 'cross_axis_width_du', 'slots']
const RESULT_KEYS = ['schema', 'operation', 'outcome', 'created', 'drawing_id', 'project_id',
  'expected_head', 'head', 'frame', 'summary', 'terrain_standing']
const HEAD_KEYS = ['schema', 'drawing_id', 'project_id', 'index', 'parent', 'state']
const REF_KEYS = ['schema', 'artifact_id', 'media_type', 'filename', 'byte_length',
  'content_sha256', 'source_version', 'download']
const FRAME_KEYS = ['coordinate_system', 'transform', 'drawing_units', 'meters_per_unit',
  'crs', 'elevation_datum', 'horizontal', 'elevation']
const HEX64 = /^[0-9a-f]{64}$/
const DECIMAL = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/
const EPSG = /^EPSG:[1-9][0-9]{0,5}$/
const DRAWING = /^[a-z0-9][a-z0-9_-]{0,62}$/

function plain(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

function onlyKeys(value, keys) {
  return Reflect.ownKeys(value).every((key) => typeof key === 'string' && keys.includes(key))
}

function exact(value, keys) {
  return plain(value) && onlyKeys(value, keys) && keys.every((key) => Object.hasOwn(value, key))
}

function integer(value, low, high) {
  return Number.isInteger(value) && value >= low && value <= high
}

function hex(value) { return typeof value === 'string' && HEX64.test(value) }
function head(value) { return value === null || hex(value) }
function scale(units) { return units === 'm' ? 1 : units === 'ft' ? 0.3048 : null }
function failure(suffix, field = null) { return { ok: false, code: `TRACKER_ROWS_${suffix}`, field } }
function copy(value, keys) { return Object.fromEntries(keys.map((key) => [key, value[key]])) }

function project(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 200 || [...value].length > 100) return false
  encodeURIComponent(value)
  return true
}

function numeric(value, draft) {
  if (draft && typeof value === 'string') {
    const trimmed = value.trim()
    return DECIMAL.test(trimmed) ? Number(trimmed) : NaN
  }
  return typeof value === 'number' ? value : NaN
}

function point(value, field, draft) {
  if (!Array.isArray(value) || value.length !== 2 || !Object.hasOwn(value, 0) || !Object.hasOwn(value, 1)) {
    return failure('ROW_INVALID', field)
  }
  const out = []
  for (let i = 0; i < 2; i += 1) {
    const number = numeric(value[i], draft)
    if (!Number.isFinite(number) || Math.abs(number) > 1e9) return failure('ROW_INVALID', `${field}.${i}`)
    out.push(number)
  }
  return { ok: true, value: out }
}

function requestCopy(source, units, draft) {
  const keys = draft ? DRAFT_KEYS : BODY_KEYS
  if (!plain(source) || !onlyKeys(source, keys)
    || !keys.filter((key) => key !== (draft ? 'expectedHead' : 'expected_head')).every((key) => Object.hasOwn(source, key))) {
    return failure('REQUEST_INVALID')
  }
  if (!draft && source.operation !== TRACKER_ROWS_OPERATION) return failure('OPERATION_UNSUPPORTED', 'operation')
  const headKey = draft ? 'expectedHead' : 'expected_head'
  if (!Object.hasOwn(source, headKey)) return failure('HEAD_INVALID', 'expected_head')
  const expectedHead = source[headKey]
  if (!head(expectedHead)) return failure('HEAD_INVALID', 'expected_head')
  const unitScale = scale(draft ? source.drawingUnits : units)
  if (unitScale === null) return failure('UNITS_UNSUPPORTED', 'drawing_units')
  const rows = source.rows
  if (!Array.isArray(rows)) return failure('ROWS_INVALID', 'rows')
  const count = rows.length
  if (!Number.isInteger(count) || count < 1) return failure('ROWS_INVALID', 'rows')
  if (count > TRACKER_ROWS_MAX_ROWS) return failure('LIMIT_EXCEEDED', 'rows')
  const power = numeric(source[draft ? 'modulePowerWatts' : 'module_power_watts'], draft)
  if (!Number.isFinite(power) || power <= 0 || power > 1e15) return failure('POWER_INVALID', 'module_power_watts')
  const normalized = []
  let total = 0
  for (let i = 0; i < count; i += 1) {
    const field = `rows.${i}`
    if (!Object.hasOwn(rows, i)) return failure('ROW_INVALID', field)
    const row = rows[i]
    if (!exact(row, ROW_KEYS)) return failure('ROW_INVALID', field)
    const start = point(row.axis_start, `${field}.axis_start`, draft)
    if (!start.ok) return start
    const end = point(row.axis_end, `${field}.axis_end`, draft)
    if (!end.ok) return end
    const slots = numeric(row.slots, draft)
    // JavaScript loses integral JSON float spelling: JSON.parse('3.0') and draft '3.0'
    // become 3 and serialize as an integer. Raw HTTP 3.0 is a Python float and R1 refuses it.
    if (!integer(slots, 1, TRACKER_ROWS_MAX_ROW_SLOTS)) return failure('SLOTS_INVALID', `${field}.slots`)
    total += slots
    if (total > TRACKER_ROWS_MAX_TOTAL_SLOTS) return failure('LIMIT_EXCEEDED', 'rows')
    const dx = end.value[0] - start.value[0]
    const dy = end.value[1] - start.value[1]
    const length = Math.sqrt(dx * dx + dy * dy)
    // R1 uses this arithmetic and tolerance. Conversion additionally owns its G2a edge floor
    // (64 x 16 ulps of the largest coordinate); short edges at large coordinates may fail there.
    if (length <= 1e-9 || length * unitScale > 100000) return failure('AXIS_INVALID', `${field}.axis_end`)
    const width = numeric(row.cross_axis_width_du, draft)
    if (!Number.isFinite(width) || width <= 1e-9 || width * unitScale > 1000) {
      return failure('WIDTH_INVALID', `${field}.cross_axis_width_du`)
    }
    normalized.push({ axis_start: start.value, axis_end: end.value, cross_axis_width_du: width, slots })
  }
  const body = { operation: TRACKER_ROWS_OPERATION, rows: normalized, module_power_watts: power, expected_head: expectedHead }
  if (new TextEncoder().encode(JSON.stringify(body)).byteLength > TRACKER_ROWS_MAX_REQUEST_BYTES) {
    return failure('LIMIT_EXCEEDED')
  }
  return { ok: true, body }
}

export function buildTrackerRowsRequest(input) {
  try {
    if (!plain(input) || !onlyKeys(input, DRAFT_KEYS)) return failure('REQUEST_INVALID')
    // Read units only after structural and head checks inside requestCopy.
    return requestCopy(input, undefined, true)
  } catch {
    return failure('REQUEST_INVALID')
  }
}

export function validateTrackerRowsRequest(body, context) {
  try {
    return requestCopy(body, context?.drawingUnits, false)
  } catch {
    return failure('REQUEST_INVALID')
  }
}

function standingCopy(value) {
  if (!exact(value, ['schema', 'maturity', 'grid_sha256', 'frames', 'piles'])) return null
  value = copy(value, ['schema', 'maturity', 'grid_sha256', 'frames', 'piles'])
  if (value.schema !== 'leaf.solar-frames-piles-terrain-standing.v1' || value.maturity !== 'preview'
    || !head(value.grid_sha256)) return null
  const out = copy(value, ['schema', 'maturity', 'grid_sha256'])
  for (const key of ['frames', 'piles']) {
    if (!exact(value[key], ['state', 'checked', 'stale'])) return null
    const entry = copy(value[key], ['state', 'checked', 'stale'])
    if (!integer(entry.checked, 0, 1000000)
      || !integer(entry.stale, 0, entry.checked)) return null
    if (entry.state === 'absent' ? entry.checked !== 0
      : entry.state === 'current' ? entry.checked === 0 || entry.stale !== 0
        : entry.state === 'stale' ? entry.stale === 0 : true) return null
    out[key] = copy(entry, ['state', 'checked', 'stale'])
  }
  return out
}

export function validateTrackerRowsResult(body, context) {
  try {
    if (!exact(context, ['drawingId', 'projectId', 'drawingUnits', 'request', 'status'])) return null
    const { drawingId, projectId, drawingUnits, status } = context
    if (typeof drawingId !== 'string' || !DRAWING.test(drawingId) || (projectId !== null && !project(projectId))) return null
    const sent = validateTrackerRowsRequest(context.request, { drawingUnits })
    if (!sent.ok || !exact(body, [...RESULT_KEYS, 'error', 'degraded_mode'])) return null
    body = copy(body, [...RESULT_KEYS, 'error', 'degraded_mode'])
    if (body.error !== null || body.degraded_mode !== false || body.schema !== TRACKER_ROWS_RESULT_SCHEMA
      || body.operation !== TRACKER_ROWS_OPERATION) return null
    if (status === 201 ? body.created !== true || body.outcome !== 'published'
      : status === 200 ? body.created !== false || body.outcome !== 'retry' : true) return null
    if (body.drawing_id !== drawingId || !project(body.project_id)
      || (projectId !== null && body.project_id !== projectId) || body.expected_head !== sent.body.expected_head) return null
    if (!exact(body.head, HEAD_KEYS)) return null
    const h = copy(body.head, HEAD_KEYS)
    if (h.schema !== 'leaf.solar-physical-head.v1' || h.drawing_id !== drawingId
      || h.project_id !== body.project_id || !integer(h.index, 0, 4095) || h.parent !== sent.body.expected_head
      || (h.index === 0 ? h.parent !== null : !hex(h.parent))) return null
    if (!exact(h.state, REF_KEYS)) return null
    const ref = copy(h.state, REF_KEYS)
    if (ref.schema !== 'leaf.solar-artifact-ref.v1' || !hex(ref.artifact_id)
      || !hex(ref.content_sha256) || ref.media_type !== 'application/json' || ref.filename !== 'physical-state.json'
      || !integer(ref.byte_length, 1, 16777216) || !Number.isSafeInteger(ref.source_version) || ref.source_version < 1
      || ref.download !== `/api/drawings/${drawingId}/artifacts/${ref.artifact_id}`) return null
    if (!exact(body.frame, FRAME_KEYS)) return null
    const frame = copy(body.frame, FRAME_KEYS)
    if (frame.coordinate_system !== 'world' || frame.transform !== 'identity'
      || frame.drawing_units !== drawingUnits || frame.meters_per_unit !== scale(drawingUnits)
      || (frame.crs !== 'none' && !(typeof frame.crs === 'string' && EPSG.test(frame.crs)))
      || (frame.elevation_datum !== 'unrecorded' && !(typeof frame.elevation_datum === 'string' && EPSG.test(frame.elevation_datum)))
      || frame.horizontal !== 'drawing-units' || frame.elevation !== 'metres') return null
    if (!exact(body.summary, ['rows', 'slots', 'module_power_watts'])) return null
    const summary = copy(body.summary, ['rows', 'slots', 'module_power_watts'])
    if (summary.rows !== sent.body.rows.length
      || summary.slots !== sent.body.rows.reduce((sum, row) => sum + row.slots, 0)
      || summary.module_power_watts !== sent.body.module_power_watts) return null
    const standing = standingCopy(body.terrain_standing)
    if (standing === null) return null
    return { ...copy(body, RESULT_KEYS), head: { ...copy(h, HEAD_KEYS), state: copy(ref, REF_KEYS) },
      frame: copy(frame, FRAME_KEYS), summary: copy(summary, ['rows', 'slots', 'module_power_watts']), terrain_standing: standing }
  } catch {
    return null
  }
}
