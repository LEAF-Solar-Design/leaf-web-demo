// The pure model of the Ground Physical terrain panel (SolarTerrainPanel.jsx) and the validators
// the terrain client (solarTerrainClient.js) answers through. No DOM, no fetch, no storage, no
// clock, and no import from the client.
//
// Routes (server/routers/solar_terrain.py over server/solar_ground_terrain_adapter.py):
// GET /api/drawings/{drawing_id}/terrain answers the stored terrain's view, and
// POST /api/drawings/{drawing_id}/terrain/operations runs mesh, slope or slope-clear on the head
// the request names. Every view and result says maturity "preview"; nothing here turns one into
// an engineering claim, and the kernel's slope status string is validated as a string and never
// placed in a display line.
//
// Fails closed: a body that is not exactly the documented shape, or that does not describe the
// scope or the request it answers, validates to null. Numbers are never coerced. Every loop is
// bounded (a grid side is at most 300, a stored record at most 65,536 nodes and 32 levels).

export const TERRAIN_MESH_CAPABILITY = 'terrain-mesh-render'
export const TERRAIN_SLOPE_CAPABILITY = 'tracker-slope-violations'
export const TERRAIN_CAPABILITIES = Object.freeze([TERRAIN_MESH_CAPABILITY, TERRAIN_SLOPE_CAPABILITY])
export const TERRAIN_OPERATIONS = Object.freeze(['mesh', 'slope', 'slope-clear'])
export const TERRAIN_OPERATION_CAPABILITIES = Object.freeze({
  mesh: TERRAIN_MESH_CAPABILITY,
  slope: TERRAIN_SLOPE_CAPABILITY,
  'slope-clear': TERRAIN_SLOPE_CAPABILITY,
})
export const TERRAIN_PREVIEW_STATES = Object.freeze(['absent', 'current', 'stale'])
// The plugin's FramePreset defaults (solar_ground_terrain.DEFAULT_PRESET_LIMITS, Columns 0), in
// the server's own key order.
export const TERRAIN_LIMIT_DEFAULTS = Object.freeze({
  MaxNsSlopePct: 8.5,
  MaxRowToRowEwSlopePct: 10,
  MaxAxialSlopePct: 8.5,
  MaxCrossAxisSlopePct: 10,
  MaxRowToRowSlopeDeg: 4,
  MaxSlopePercent: 15,
  Columns: 0,
})
export const TERRAIN_LIMIT_KEYS = Object.freeze(Object.keys(TERRAIN_LIMIT_DEFAULTS))
export const TERRAIN_MAX_LIMIT_PERCENT = 1000
export const TERRAIN_MAX_LIMIT_DEGREES = 90
export const TERRAIN_MAX_COLUMNS = 10_000
export const TERRAIN_MAX_GRID_SIDE = 300
// The longest slope limit draft the model reads. A form rule, not a server limit.
export const TERRAIN_MAX_DRAFT_CHARS = 64
export const TERRAIN_EMPTY_SENTENCE = 'No terrain is stored for this drawing'
export const TERRAIN_NO_GRID_SENTENCE = 'This terrain state has no grid'
export const TERRAIN_DRAFTS_SENTENCE = 'The slope limits could not be read, so enter them again'

function limitMax(key) {
  if (key === 'Columns') return TERRAIN_MAX_COLUMNS
  return key === 'MaxRowToRowSlopeDeg' ? TERRAIN_MAX_LIMIT_DEGREES : TERRAIN_MAX_LIMIT_PERCENT
}

function limitField(key, label, kind) {
  const max = limitMax(key)
  return Object.freeze({
    key,
    label,
    kind,
    max,
    hint: `0 to ${max}, blank uses ${TERRAIN_LIMIT_DEFAULTS[key]}`,
    reason: kind === 'columns' ? `Enter a whole number from 0 to ${max}` : `Enter a decimal number from 0 to ${max}`,
  })
}

// The slope form's seven fields, in the server's key order. These are the limits of the NEXT
// slope preview: editing one never changes the standing of a stored preview.
export const TERRAIN_LIMIT_FIELDS = Object.freeze([
  limitField('MaxNsSlopePct', 'North to south slope limit, percent', 'percent'),
  limitField('MaxRowToRowEwSlopePct', 'Row to row east to west slope limit, percent', 'percent'),
  limitField('MaxAxialSlopePct', 'Axial slope limit, percent', 'percent'),
  limitField('MaxCrossAxisSlopePct', 'Cross axis slope limit, percent', 'percent'),
  limitField('MaxRowToRowSlopeDeg', 'Row to row slope limit, degrees', 'degrees'),
  limitField('MaxSlopePercent', 'Overall slope limit, percent', 'percent'),
  limitField('Columns', 'Columns', 'columns'),
])

const VIEW_RESPONSE_SCHEMA = 'leaf.solar-terrain-view-response.v1'
const VIEW_SCHEMA = 'leaf.solar-terrain-view.v1'
const OPERATION_SCHEMA = 'leaf.solar-terrain-operation.v1'
const RECORD_SCHEMA = 'leaf.solar-terrain-preview.v1'
const HEAD_SCHEMA = 'leaf.solar-physical-head.v1'
const REF_SCHEMA = 'leaf.solar-artifact-ref.v1'
const MATURITY = 'preview'

const DRAWING_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/
const EPSG_PATTERN = /^EPSG:[1-9][0-9]{0,5}$/
const HEX64_PATTERN = /^[0-9a-f]{64}$/
const DECIMAL_PATTERN = /^[0-9]+(?:\.[0-9]+)?$/
const DIGITS_PATTERN = /^[0-9]+$/
const MAX_PROJECT_ID_CHARS = 100
const MAX_HEAD_INDEX = 4095
const MAX_ARTIFACT_BYTES = 16_777_216
const MAX_MESH_NODES = 90_000
// (300 - 1) squared: one face per cell of the largest grid.
const MAX_MESH_FACES = 89_401
const MAX_ABS_FLOAT = 1e15
// solar_physical_state.MAX_COUNTER (mesh_faces) and its node budget (the marker list).
const MAX_COUNTER = 1_000_000_000
const MAX_MARKERS = 1_000_000
const MAX_FRAMES = 20_000
const MAX_STATUS_CHARS = 65_536
const MAX_RECORD_DEPTH = 32
const MAX_RECORD_NODES = 65_536
// Metres per drawing unit (solar_physical_state.UNITS), the only values a frame can carry.
const METERS_PER_UNIT = Object.freeze({ m: 1, ft: 0.3048 })
const UNIT_LABELS = Object.freeze({ m: 'Meters', ft: 'Feet' })
const UNIT_WORDS = Object.freeze({ m: 'meters', ft: 'feet' })

const ENVELOPE_KEYS = Object.freeze(['error', 'degraded_mode'])
const VIEW_RESPONSE_KEYS = Object.freeze(['schema', 'stored', 'head', 'terrain'])
const HEAD_KEYS = Object.freeze(['schema', 'drawing_id', 'project_id', 'index', 'parent', 'state'])
const REF_KEYS = Object.freeze([
  'schema', 'artifact_id', 'media_type', 'filename', 'byte_length', 'content_sha256', 'source_version', 'download',
])
const VIEW_KEYS = Object.freeze([
  'schema', 'maturity', 'drawing_id', 'project_id', 'frame', 'grid', 'mesh_faces', 'slope_markers', 'previews',
])
const FRAME_KEYS = Object.freeze([
  'coordinate_system', 'transform', 'drawing_units', 'meters_per_unit', 'crs', 'elevation_datum', 'horizontal',
  'elevation',
])
const GRID_KEYS = Object.freeze([
  'rows', 'cols', 'x_min', 'x_max', 'y_min', 'y_max', 'cell_x', 'cell_y', 'cell_x_m', 'cell_y_m',
  'elevation_min_m', 'elevation_max_m', 'grid_sha256',
])
const GRID_NUMBER_KEYS = Object.freeze(['x_min', 'x_max', 'y_min', 'y_max', 'elevation_min_m', 'elevation_max_m'])
const PREVIEW_KEYS = Object.freeze(['state', 'record'])
const MESH_RECORD_KEYS = Object.freeze([
  'schema', 'capability', 'maturity', 'grid_sha256', 'meters_per_unit', 'faces', 'buckets', 'max_slope_percent',
  'mesh_sha256',
])
const BUCKET_KEYS = Object.freeze(['Green', 'Yellow', 'Red'])
const SLOPE_RECORD_KEYS = Object.freeze([
  'schema', 'capability', 'maturity', 'grid_sha256', 'meters_per_unit', 'limits', 'frames', 'markers', 'status',
  'report_sha256',
])
const RESULT_KEYS = Object.freeze([
  'schema', 'maturity', 'operation', 'capability', 'created', 'drawing_id', 'project_id', 'frame', 'grid', 'record',
  'replaced', 'head',
])
const REPORT_COUNT_KEYS = Object.freeze([
  'tracker_count', 'axial_rows_checked', 'axial_violation_rows', 'cross_axis_pairs_checked',
  'cross_axis_violation_pairs', 'row_to_row_pairs_checked', 'row_to_row_angle_violation_pairs',
  'trackers_needing_terrain_following',
])
const REPORT_KEYS = Object.freeze([...REPORT_COUNT_KEYS, 'has_violations', 'status'])

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

// True when the own keys are exactly `required` plus any subset of `optional`.
function hasExactKeys(value, required, optional = []) {
  for (const key of Reflect.ownKeys(value)) {
    if (typeof key !== 'string') return false
    if (!required.includes(key) && !optional.includes(key)) return false
  }
  return required.every((key) => Object.hasOwn(value, key))
}

function isIntegerIn(value, low, high) {
  return Number.isInteger(value) && value >= low && value <= high
}

function isDrawingId(value) {
  return typeof value === 'string' && DRAWING_ID_PATTERN.test(value)
}

// 1 to 100 code points, the route's own bound (Python len counts code points).
function isProjectId(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 2 * MAX_PROJECT_ID_CHARS) return false
  return [...value].length <= MAX_PROJECT_ID_CHARS
}

function isHex64(value) {
  return typeof value === 'string' && HEX64_PATTERN.test(value)
}

function isEpsg(value) {
  return typeof value === 'string' && EPSG_PATTERN.test(value)
}

function isBounded(value) {
  return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= MAX_ABS_FLOAT
}

function isCell(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= MAX_ABS_FLOAT
}

function isUnitScale(value) {
  return value === METERS_PER_UNIT.m || value === METERS_PER_UNIT.ft
}

function copyOf(value, keys) {
  const copy = {}
  for (const key of keys) copy[key] = value[key]
  return copy
}

// At most three decimals and no trailing zeros (String(-0) is '0').
function formatNumber(value) {
  return String(Number(value.toFixed(3)))
}

function counted(count, one, many) {
  return `${count} ${count === 1 ? one : many}`
}

// The complete seven-key limits a slope preview uses: the defaults for every key not given.
// Closed keys; every percent a finite number in 0..1000, degrees in 0..90, Columns an integer
// in 0..10,000. A boolean, a numeric string or a non-finite number is refused, never coerced;
// negative zero is zero. Null for anything else.
export function resolveTerrainLimits(limits) {
  try {
    const resolved = { ...TERRAIN_LIMIT_DEFAULTS }
    if (limits === undefined || limits === null) return resolved
    if (!isPlainObject(limits)) return null
    for (const key of Reflect.ownKeys(limits)) {
      if (typeof key !== 'string' || !TERRAIN_LIMIT_KEYS.includes(key)) return null
      const value = limits[key]
      if (typeof value !== 'number' || !Number.isFinite(value)) return null
      if (key === 'Columns' && !Number.isInteger(value)) return null
      if (value < 0 || value > limitMax(key)) return null
      resolved[key] = value === 0 ? 0 : value
    }
    return resolved
  } catch {
    return null
  }
}

// One draft: undefined when blank (the default applies), null when it is not a plain literal
// inside the field's range, else its number.
function parseDraft(draft, field) {
  if (typeof draft !== 'string' || draft.length > TERRAIN_MAX_DRAFT_CHARS) return null
  const trimmed = draft.trim()
  if (trimmed === '') return undefined
  if (!(field.kind === 'columns' ? DIGITS_PATTERN : DECIMAL_PATTERN).test(trimmed)) return null
  const value = Number(trimmed)
  return Number.isFinite(value) && value <= field.max ? value : null
}

// The slope form's text drafts as typed limits: { ok: true, limits } with the complete seven-key
// object, or { ok: false, field, reason, invalid } naming the first field that cannot be read
// and every field's reason. Blank means the default; a percent or degree draft is a plain
// decimal literal and Columns is decimal digits, each at most 64 characters.
export function parseTerrainLimitDrafts(drafts) {
  try {
    const source = drafts === undefined || drafts === null ? {} : drafts
    if (!isPlainObject(source) || !hasExactKeys(source, [], TERRAIN_LIMIT_KEYS)) {
      return Object.freeze({ ok: false, field: null, reason: TERRAIN_DRAFTS_SENTENCE, invalid: Object.freeze({}) })
    }
    const typed = {}
    const invalid = {}
    let first = null
    for (const field of TERRAIN_LIMIT_FIELDS) {
      const parsed = parseDraft(Object.hasOwn(source, field.key) ? source[field.key] : '', field)
      if (parsed === null) {
        invalid[field.key] = field.reason
        if (first === null) first = field
      } else if (parsed !== undefined) {
        typed[field.key] = parsed
      }
    }
    if (first !== null) {
      return Object.freeze({ ok: false, field: first.key, reason: first.reason, invalid: Object.freeze(invalid) })
    }
    const limits = resolveTerrainLimits(typed)
    if (limits !== null) return Object.freeze({ ok: true, limits: Object.freeze(limits) })
  } catch {
    // Falls through to the closed refusal.
  }
  return Object.freeze({ ok: false, field: null, reason: TERRAIN_DRAFTS_SENTENCE, invalid: Object.freeze({}) })
}

function validEnvelope(body) {
  if (Object.hasOwn(body, 'error') && body.error !== null) return false
  return !Object.hasOwn(body, 'degraded_mode') || body.degraded_mode === false
}

function refCopy(ref, drawingId) {
  if (!isPlainObject(ref) || !hasExactKeys(ref, REF_KEYS)) return null
  if (ref.schema !== REF_SCHEMA || !isHex64(ref.artifact_id) || !isHex64(ref.content_sha256)) return null
  if (ref.media_type !== 'application/json' || ref.filename !== 'physical-state.json') return null
  if (!isIntegerIn(ref.byte_length, 1, MAX_ARTIFACT_BYTES)) return null
  // A positive drawing revision this page can represent exactly; the route sets no smaller ceiling.
  if (!Number.isSafeInteger(ref.source_version) || ref.source_version < 1) return null
  if (ref.download !== `/api/drawings/${drawingId}/artifacts/${ref.artifact_id}`) return null
  return copyOf(ref, REF_KEYS)
}

// The head copy, or null. `projectId` null accepts any valid project the head names.
function headCopy(value, drawingId, projectId) {
  if (!isPlainObject(value) || !hasExactKeys(value, HEAD_KEYS)) return null
  if (value.schema !== HEAD_SCHEMA || value.drawing_id !== drawingId || !isProjectId(value.project_id)) return null
  if (projectId !== null && value.project_id !== projectId) return null
  if (!isIntegerIn(value.index, 0, MAX_HEAD_INDEX)) return null
  if (value.index === 0 ? value.parent !== null : !isHex64(value.parent)) return null
  const state = refCopy(value.state, drawingId)
  if (state === null) return null
  return { ...copyOf(value, HEAD_KEYS), state }
}

function frameCopy(value) {
  if (!isPlainObject(value) || !hasExactKeys(value, FRAME_KEYS)) return null
  if (value.coordinate_system !== 'world' || value.transform !== 'identity') return null
  if (typeof value.drawing_units !== 'string' || !Object.hasOwn(METERS_PER_UNIT, value.drawing_units)) return null
  if (value.meters_per_unit !== METERS_PER_UNIT[value.drawing_units]) return null
  if (value.crs !== 'none' && !isEpsg(value.crs)) return null
  if (value.elevation_datum !== 'unrecorded' && !isEpsg(value.elevation_datum)) return null
  if (value.horizontal !== 'drawing-units' || value.elevation !== 'metres') return null
  return copyOf(value, FRAME_KEYS)
}

// The grid summary copy, or null. Mirrors the adapter's grid_summary and the checks of its
// document_grid: every cell is recomputed from the bounds, and every cell origin must still
// advance after float rounding (a positive overall extent alone is not enough).
function gridCopy(value, metersPerUnit) {
  if (!isPlainObject(value) || !hasExactKeys(value, GRID_KEYS)) return null
  const { rows, cols } = value
  if (!isIntegerIn(rows, 2, TERRAIN_MAX_GRID_SIDE) || !isIntegerIn(cols, 2, TERRAIN_MAX_GRID_SIDE)) return null
  if (rows * cols > MAX_MESH_NODES) return null
  if (!GRID_NUMBER_KEYS.every((key) => isBounded(value[key]))) return null
  if (!(value.x_min < value.x_max && value.y_min < value.y_max)) return null
  if (value.elevation_min_m > value.elevation_max_m) return null
  const cellX = (value.x_max - value.x_min) / (cols - 1)
  const cellY = (value.y_max - value.y_min) / (rows - 1)
  if (!isCell(cellX) || !isCell(cellY) || value.cell_x !== cellX || value.cell_y !== cellY) return null
  const cellXMeters = cellX * metersPerUnit
  const cellYMeters = cellY * metersPerUnit
  if (!isCell(cellXMeters) || !isCell(cellYMeters)) return null
  if (value.cell_x_m !== cellXMeters || value.cell_y_m !== cellYMeters) return null
  for (let col = 0; col < cols - 1; col += 1) {
    const origin = value.x_min + col * cellX
    if (!(origin + cellX > origin)) return null
  }
  for (let row = 0; row < rows - 1; row += 1) {
    const origin = value.y_min + row * cellY
    if (!(origin + cellY > origin)) return null
  }
  if (!isHex64(value.grid_sha256)) return null
  return copyOf(value, GRID_KEYS)
}

const NOT_JSON = Symbol('not-json')

// A bounded deep copy of a stored record, which the GET route passes through unvalidated.
function cloneJson(value, depth, budget) {
  budget.nodes += 1
  if (budget.nodes > MAX_RECORD_NODES) return NOT_JSON
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) ? value : NOT_JSON
  if (depth >= MAX_RECORD_DEPTH) return NOT_JSON
  if (Array.isArray(value)) {
    const items = []
    for (const item of value) {
      const copy = cloneJson(item, depth + 1, budget)
      if (copy === NOT_JSON) return NOT_JSON
      items.push(copy)
    }
    return items
  }
  if (!isPlainObject(value)) return NOT_JSON
  const out = {}
  for (const key of Reflect.ownKeys(value)) {
    if (typeof key !== 'string') return NOT_JSON
    const copy = cloneJson(value[key], depth + 1, budget)
    if (copy === NOT_JSON) return NOT_JSON
    Object.defineProperty(out, key, { value: copy, enumerable: true, writable: true, configurable: true })
  }
  return out
}

// The numbers a mesh record may show, or null when the record is not exactly a mesh record.
function meshMetrics(record) {
  if (!isPlainObject(record) || !hasExactKeys(record, MESH_RECORD_KEYS)) return null
  if (record.schema !== RECORD_SCHEMA || record.capability !== TERRAIN_MESH_CAPABILITY) return null
  if (record.maturity !== MATURITY || !isHex64(record.grid_sha256) || !isHex64(record.mesh_sha256)) return null
  if (!isUnitScale(record.meters_per_unit) || !isIntegerIn(record.faces, 1, MAX_MESH_FACES)) return null
  const { buckets } = record
  if (!isPlainObject(buckets) || !hasExactKeys(buckets, BUCKET_KEYS)) return null
  if (!BUCKET_KEYS.every((key) => isIntegerIn(buckets[key], 0, record.faces))) return null
  if (buckets.Green + buckets.Yellow + buckets.Red !== record.faces) return null
  const steepest = record.max_slope_percent
  if (typeof steepest !== 'number' || !Number.isFinite(steepest) || steepest < 0) return null
  return Object.freeze({
    faces: record.faces, green: buckets.Green, yellow: buckets.Yellow, red: buckets.Red, maxSlopePercent: steepest,
  })
}

function completeLimits(value) {
  if (!isPlainObject(value) || !hasExactKeys(value, TERRAIN_LIMIT_KEYS)) return null
  return resolveTerrainLimits(value)
}

// The numbers a slope record may show, or null. Its `status` is checked as a bounded string
// and left out: it is the kernel's wording, never this page's.
function slopeMetrics(record) {
  if (!isPlainObject(record) || !hasExactKeys(record, SLOPE_RECORD_KEYS)) return null
  if (record.schema !== RECORD_SCHEMA || record.capability !== TERRAIN_SLOPE_CAPABILITY) return null
  if (record.maturity !== MATURITY || !isHex64(record.grid_sha256) || !isHex64(record.report_sha256)) return null
  if (!isUnitScale(record.meters_per_unit) || completeLimits(record.limits) === null) return null
  if (!isIntegerIn(record.frames, 0, MAX_FRAMES) || !isIntegerIn(record.markers, 0, MAX_FRAMES)) return null
  if (typeof record.status !== 'string' || record.status.length > MAX_STATUS_CHARS) return null
  return Object.freeze({ frames: record.frames, markers: record.markers })
}

// Python's record.get(key): the value, or None for a key the record does not carry.
function pick(record, key) {
  return Object.hasOwn(record, key) ? record[key] : null
}

// Python compares booleans as 0 or 1 against numbers in the stored-preview standing check.
function pyEquals(a, b) {
  if (typeof a === 'boolean' && typeof b === 'number') return Number(a) === b
  if (typeof b === 'boolean' && typeof a === 'number') return a === Number(b)
  return a === b
}

// The two preview wrappers, or null. "current" must hold the adapter's own three comparisons
// (grid digest, metres per unit, stored count); "stale" keeps whatever was stored, so an old
// malformed record stays visible as stale instead of hiding the head.
function previewsCopy(value, grid, metersPerUnit, counts) {
  if (!isPlainObject(value) || !hasExactKeys(value, TERRAIN_CAPABILITIES)) return null
  const digest = grid === null ? null : grid.grid_sha256
  const budget = { nodes: 0 }
  const out = {}
  for (const capability of TERRAIN_CAPABILITIES) {
    const wrapper = value[capability]
    if (!isPlainObject(wrapper) || !hasExactKeys(wrapper, PREVIEW_KEYS)) return null
    const { state, record } = wrapper
    if (typeof state !== 'string' || !TERRAIN_PREVIEW_STATES.includes(state)) return null
    if ((state === 'absent') !== (record === null)) return null
    if (state === 'current') {
      if (!isPlainObject(record) || !pyEquals(pick(record, 'grid_sha256'), digest)) return null
      if (!pyEquals(pick(record, 'meters_per_unit'), metersPerUnit)) return null
      const stored = Object.hasOwn(record, 'faces') ? record.faces : pick(record, 'markers')
      if (!pyEquals(stored, counts[capability])) return null
    }
    const copy = cloneJson(record, 0, budget)
    if (copy === NOT_JSON) return null
    out[capability] = { state, record: copy }
  }
  return out
}

function terrainCopy(value, drawingId, projectId) {
  if (!isPlainObject(value) || !hasExactKeys(value, VIEW_KEYS)) return null
  if (value.schema !== VIEW_SCHEMA || value.maturity !== MATURITY) return null
  if (value.drawing_id !== drawingId || value.project_id !== projectId) return null
  const frame = frameCopy(value.frame)
  if (frame === null) return null
  let grid = null
  if (value.grid !== null) {
    grid = gridCopy(value.grid, frame.meters_per_unit)
    if (grid === null) return null
  }
  if (!isIntegerIn(value.mesh_faces, 0, MAX_COUNTER) || !isIntegerIn(value.slope_markers, 0, MAX_MARKERS)) return null
  const previews = previewsCopy(value.previews, grid, frame.meters_per_unit, {
    [TERRAIN_MESH_CAPABILITY]: value.mesh_faces,
    [TERRAIN_SLOPE_CAPABILITY]: value.slope_markers,
  })
  if (previews === null) return null
  return {
    schema: value.schema,
    maturity: value.maturity,
    drawing_id: value.drawing_id,
    project_id: value.project_id,
    frame,
    grid,
    mesh_faces: value.mesh_faces,
    slope_markers: value.slope_markers,
    previews,
  }
}

// The normalized terrain view (4 keys, envelope fields dropped) or null. `scope` is the scope
// it was read for: { drawingId, projectId }. A drawing with no stored terrain is a success
// (stored false, head and terrain null), and so is a head without a grid.
export function validateTerrainView(body, scope) {
  try {
    if (!isPlainObject(scope)) return null
    const { drawingId, projectId = null } = scope
    if (!isDrawingId(drawingId) || (projectId !== null && !isProjectId(projectId))) return null
    if (!isPlainObject(body) || !hasExactKeys(body, VIEW_RESPONSE_KEYS, ENVELOPE_KEYS)) return null
    if (!validEnvelope(body) || body.schema !== VIEW_RESPONSE_SCHEMA || typeof body.stored !== 'boolean') return null
    if (!body.stored) {
      if (body.head !== null || body.terrain !== null) return null
      return { schema: body.schema, stored: false, head: null, terrain: null }
    }
    const head = headCopy(body.head, drawingId, projectId)
    if (head === null) return null
    const terrain = terrainCopy(body.terrain, drawingId, head.project_id)
    if (terrain === null) return null
    return { schema: body.schema, stored: true, head, terrain }
  } catch {
    return null
  }
}

function reportCopy(value) {
  if (!isPlainObject(value) || !hasExactKeys(value, REPORT_KEYS)) return null
  if (!REPORT_COUNT_KEYS.every((key) => isIntegerIn(value[key], 0, MAX_FRAMES))) return null
  const rows = value.tracker_count
  if (value.axial_rows_checked > rows || value.axial_violation_rows > value.axial_rows_checked) return null
  // A row takes at most one cross-axis partner, so the pairs never outnumber the rows.
  if (value.cross_axis_pairs_checked > rows || value.row_to_row_pairs_checked > rows) return null
  if (value.cross_axis_violation_pairs > value.cross_axis_pairs_checked) return null
  if (value.row_to_row_angle_violation_pairs > value.row_to_row_pairs_checked) return null
  if (value.trackers_needing_terrain_following > rows) return null
  const violations = value.axial_violation_rows > 0 || value.cross_axis_violation_pairs > 0
    || value.row_to_row_angle_violation_pairs > 0
  if (value.has_violations !== violations) return null
  if (typeof value.status !== 'string' || value.status.length > MAX_STATUS_CHARS) return null
  return copyOf(value, REPORT_KEYS)
}

// The normalized operation result (envelope fields dropped) or null. `request` is the request
// it answers: { drawingId, projectId, operation, expectedHead, limits }. `limits`, when given,
// is what a slope request sent, and the record must carry exactly that. A result that
// published nothing (created false) is a success; its head is the head the request named or
// the identical child another writer published first.
export function validateTerrainOperation(body, request) {
  try {
    if (!isPlainObject(request)) return null
    const { drawingId, projectId = null, operation, expectedHead, limits } = request
    if (!isDrawingId(drawingId) || (projectId !== null && !isProjectId(projectId))) return null
    if (typeof operation !== 'string' || !TERRAIN_OPERATIONS.includes(operation) || !isHex64(expectedHead)) return null
    let sent = null
    if (limits !== undefined) {
      if (operation !== 'slope') return null
      sent = resolveTerrainLimits(limits)
      if (sent === null) return null
    }
    // Only a slope result carries a report, and it always does.
    const required = operation === 'slope' ? [...RESULT_KEYS, 'report'] : RESULT_KEYS
    if (!isPlainObject(body) || !hasExactKeys(body, required, ENVELOPE_KEYS) || !validEnvelope(body)) return null
    if (body.schema !== OPERATION_SCHEMA || body.maturity !== MATURITY || body.operation !== operation) return null
    if (body.capability !== TERRAIN_OPERATION_CAPABILITIES[operation] || typeof body.created !== 'boolean') return null
    if (body.drawing_id !== drawingId || !isProjectId(body.project_id)) return null
    if (projectId !== null && body.project_id !== projectId) return null
    const frame = frameCopy(body.frame)
    if (frame === null) return null
    const head = headCopy(body.head, drawingId, body.project_id)
    if (head === null) return null
    if (body.created ? head.parent !== expectedHead
      : (head.state.artifact_id !== expectedHead && head.parent !== expectedHead)) return null
    const result = {
      schema: body.schema,
      maturity: body.maturity,
      operation: body.operation,
      capability: body.capability,
      created: body.created,
      drawing_id: body.drawing_id,
      project_id: body.project_id,
      frame,
      grid: null,
      record: null,
      replaced: body.replaced,
    }
    if (operation === 'slope-clear') {
      if (body.grid !== null || body.record !== null || !isIntegerIn(body.replaced, 0, MAX_MARKERS)) return null
      return { ...result, head }
    }
    const grid = gridCopy(body.grid, frame.meters_per_unit)
    if (grid === null) return null
    const { record } = body
    const metrics = operation === 'mesh' ? meshMetrics(record) : slopeMetrics(record)
    if (metrics === null || record.grid_sha256 !== grid.grid_sha256) return null
    if (record.meters_per_unit !== frame.meters_per_unit) return null
    result.grid = grid
    if (operation === 'mesh') {
      if (record.faces !== (grid.rows - 1) * (grid.cols - 1) || !isIntegerIn(body.replaced, 0, MAX_COUNTER)) return null
      result.record = { ...copyOf(record, MESH_RECORD_KEYS), buckets: copyOf(record.buckets, BUCKET_KEYS) }
      return { ...result, head }
    }
    if (!isIntegerIn(body.replaced, 0, MAX_MARKERS)) return null
    const recordLimits = completeLimits(record.limits)
    if (sent !== null && !TERRAIN_LIMIT_KEYS.every((key) => recordLimits[key] === sent[key])) return null
    const report = reportCopy(body.report)
    if (report === null || record.markers > report.tracker_count) return null
    if (!(report.axial_rows_checked > 0 || report.cross_axis_pairs_checked > 0
      || report.row_to_row_pairs_checked > 0)) return null
    if (report.tracker_count > record.frames) return null
    result.record = { ...copyOf(record, SLOPE_RECORD_KEYS), limits: copyOf(record.limits, TERRAIN_LIMIT_KEYS) }
    return { ...result, report, head }
  } catch {
    return null
  }
}

const refuseBuild = (reason) => Object.freeze({ ok: false, reason })

// What the panel sends for one action on the view it DISPLAYS: { ok: true, request } with the
// operation, the displayed head's state artifact as expectedHead and, for slope only, the
// complete resolved limits; or { ok: false, reason } naming the refusal in the route's own
// order. Slope-clear needs no grid.
export function buildTerrainOperation(input) {
  try {
    if (!isPlainObject(input)) return refuseBuild('TERRAIN_CLIENT_REQUEST_INVALID')
    const { view, operation, limits } = input
    if (limits !== undefined && operation !== 'slope') return refuseBuild('TERRAIN_BODY_INVALID')
    if (typeof operation !== 'string' || !TERRAIN_OPERATIONS.includes(operation)) {
      return refuseBuild('TERRAIN_OPERATION_INVALID')
    }
    if (!isPlainObject(view) || typeof view.stored !== 'boolean') return refuseBuild('TERRAIN_CLIENT_REQUEST_INVALID')
    if (!view.stored) return refuseBuild('TERRAIN_STATE_NOT_FOUND')
    if (!isPlainObject(view.head) || !isPlainObject(view.head.state) || !isPlainObject(view.terrain)) {
      return refuseBuild('TERRAIN_CLIENT_REQUEST_INVALID')
    }
    const expectedHead = view.head.state.artifact_id
    if (!isHex64(expectedHead)) return refuseBuild('TERRAIN_EXPECTED_HEAD_INVALID')
    if (operation !== 'slope-clear' && !isPlainObject(view.terrain.grid)) return refuseBuild('TERRAIN_GRID_MISSING')
    if (operation !== 'slope') return Object.freeze({ ok: true, request: Object.freeze({ operation, expectedHead }) })
    const resolved = resolveTerrainLimits(limits)
    if (resolved === null) return refuseBuild('TERRAIN_LIMITS_INVALID')
    return Object.freeze({
      ok: true, request: Object.freeze({ operation, expectedHead, limits: Object.freeze(resolved) }),
    })
  } catch {
    return refuseBuild('TERRAIN_CLIENT_REQUEST_INVALID')
  }
}

const PREVIEW_NOUNS = Object.freeze({
  [TERRAIN_MESH_CAPABILITY]: Object.freeze({ label: 'Mesh preview', absent: 'No mesh preview has been recorded' }),
  [TERRAIN_SLOPE_CAPABILITY]: Object.freeze({ label: 'Slope preview', absent: 'No slope preview has been recorded' }),
})

function meshDetails(metrics) {
  return [
    counted(metrics.faces, 'face', 'faces'),
    `${metrics.green} green`,
    `${metrics.yellow} yellow`,
    `${metrics.red} red`,
    `steepest ${formatNumber(metrics.maxSlopePercent)} percent`,
  ]
}

function slopeDetails(metrics) {
  return [
    `${counted(metrics.frames, 'tracker frame', 'tracker frames')} read`,
    `${counted(metrics.markers, 'row', 'rows')} over the limits of this preview`,
  ]
}

// One preview of a validated view, as the server stands it: { capability, state, label, text,
// standing, details, metrics }. The state is the server's and is never promoted: a stale
// preview stays stale, and its numbers are labelled as earlier results. Numbers are shown only
// from a record that is exactly a record of that capability (and, when current, one that fits
// the view's own grid); anything else shows its standing with no numbers. Null for an input
// that is not a stored view or a known capability.
export function terrainPreviewSummary(view, capability) {
  try {
    if (typeof capability !== 'string' || !TERRAIN_CAPABILITIES.includes(capability)) return null
    if (!isPlainObject(view) || view.stored !== true || !isPlainObject(view.terrain)) return null
    const { terrain } = view
    if (!isPlainObject(terrain.previews) || !isPlainObject(terrain.frame)) return null
    const wrapper = terrain.previews[capability]
    if (!isPlainObject(wrapper) || typeof wrapper.state !== 'string') return null
    if (!TERRAIN_PREVIEW_STATES.includes(wrapper.state)) return null
    const { label, absent } = PREVIEW_NOUNS[capability]
    const { state, record } = wrapper
    const summary = (text, standing, details, metrics) => Object.freeze({
      capability, state, label, text, standing, details: Object.freeze(details), metrics,
    })
    if (state === 'absent') return record === null ? summary(absent, 'Not recorded', [], null) : null
    const mesh = capability === TERRAIN_MESH_CAPABILITY
    let metrics = mesh ? meshMetrics(record) : slopeMetrics(record)
    if (metrics !== null && state === 'current') {
      const { grid } = terrain
      const count = mesh ? terrain.mesh_faces : terrain.slope_markers
      const fits = isPlainObject(grid) && record.grid_sha256 === grid.grid_sha256
        && record.meters_per_unit === terrain.frame.meters_per_unit
        && (mesh ? metrics.faces : metrics.markers) === count
        && (!mesh || metrics.faces === (grid.rows - 1) * (grid.cols - 1))
      if (!fits) metrics = null
    }
    const word = state === 'current' ? 'Current' : 'Stale'
    if (metrics === null) return summary(`${label}: ${state}`, `${word}, details unavailable`, [], null)
    const details = mesh ? meshDetails(metrics) : slopeDetails(metrics)
    const numbers = details.join(', ')
    const standing = state === 'current' ? `${word}: ${numbers}` : `${word}, earlier results: ${numbers}`
    return summary(`${label}: ${state}`, standing, details, metrics)
  } catch {
    return null
  }
}

// The lines the panel shows for a validated view: the head, the frame, the grid and both
// previews, each { key, label, text } (a preview line also carries its state). Null when a
// field it would show is missing or outside its range.
export function terrainSummary(view) {
  try {
    if (!isPlainObject(view) || typeof view.stored !== 'boolean') return null
    const line = (key, label, text, state) => Object.freeze(
      state === undefined ? { key, label, text } : { key, label, text, state })
    if (!view.stored) {
      if (view.head !== null || view.terrain !== null) return null
      return Object.freeze([line('terrain', 'Terrain', TERRAIN_EMPTY_SENTENCE)])
    }
    const { head, terrain } = view
    if (!isPlainObject(head) || !isPlainObject(head.state) || !isPlainObject(terrain)) return null
    if (!isHex64(head.state.artifact_id) || !isIntegerIn(head.index, 0, MAX_HEAD_INDEX)) return null
    const { frame, grid } = terrain
    if (!isPlainObject(frame) || typeof frame.drawing_units !== 'string') return null
    if (!Object.hasOwn(UNIT_LABELS, frame.drawing_units)) return null
    if (frame.crs !== 'none' && !isEpsg(frame.crs)) return null
    if (frame.elevation_datum !== 'unrecorded' && !isEpsg(frame.elevation_datum)) return null
    const lines = [
      line('head', 'Terrain state', head.state.artifact_id),
      line('change', 'Terrain change', `${head.index + 1} of this drawing`),
      line('units', 'Drawing units', UNIT_LABELS[frame.drawing_units]),
      line('crs', 'Coordinate system', frame.crs === 'none' ? 'None' : frame.crs),
      line('datum', 'Elevation datum', frame.elevation_datum === 'unrecorded' ? 'Not recorded' : frame.elevation_datum),
    ]
    if (grid === null) {
      lines.push(line('grid', 'Terrain grid', TERRAIN_NO_GRID_SENTENCE))
    } else {
      if (!isPlainObject(grid) || !isIntegerIn(grid.rows, 2, TERRAIN_MAX_GRID_SIDE)) return null
      if (!isIntegerIn(grid.cols, 2, TERRAIN_MAX_GRID_SIDE) || !isCell(grid.cell_x) || !isCell(grid.cell_y)) return null
      lines.push(line('grid', 'Terrain grid', `${grid.rows} by ${grid.cols} nodes`))
      lines.push(line('cell', 'Grid cell',
        `X ${formatNumber(grid.cell_x)} by Y ${formatNumber(grid.cell_y)} ${UNIT_WORDS[frame.drawing_units]}`))
    }
    for (const [key, capability] of [['mesh', TERRAIN_MESH_CAPABILITY], ['slope', TERRAIN_SLOPE_CAPABILITY]]) {
      const preview = terrainPreviewSummary(view, capability)
      if (preview === null) return null
      lines.push(line(key, preview.label, preview.standing, preview.state))
    }
    return Object.freeze(lines)
  } catch {
    return null
  }
}
