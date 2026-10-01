// The pure model of the LandXML terrain upload control (SolarLandxmlUpload.jsx). No DOM, no
// fetch, no storage. It decides, before anything is sent, the refusals the upload client
// (solarLandxmlClient.js) decides itself, in the client's own order and with the client's own
// codes and bounds, so the control can say why Import is off. It also turns the client's result
// into what the control shows: the stored terrain's summary lines or one refusal sentence.
// Fails closed: a result that is not exactly a success or a coded refusal is shown as
// LANDXML_CLIENT_RESPONSE_INVALID, never as a blank success.
import {
  LANDXML_DEFAULT_TARGET_CELLS, LANDXML_DRAWING_UNITS, LANDXML_MAX_BYTES,
  LANDXML_MAX_TARGET_CELLS, LANDXML_MIN_TARGET_CELLS, landxmlReason,
} from './solarLandxmlClient.js'

// The control's own reasons. Every other reason it shows is a code of the upload client.
export const LANDXML_UPLOAD_REASONS = Object.freeze({
  file_required: 'Choose a LandXML file to import',
  checkout_required: 'Take the drawing checkout before importing terrain',
  run_in_progress: 'A run is in progress, so wait for it to finish',
  upload_in_progress: 'The LandXML file is being imported',
  upload_cancelled: 'The import was cancelled here, but the drawing may already hold this terrain',
})
export const LANDXML_UPLOAD_UNIT_LABELS = Object.freeze({ m: 'Meters', ft: 'Feet' })
export const LANDXML_UPLOAD_DEFAULT_CELLS = String(LANDXML_DEFAULT_TARGET_CELLS)
// The longest coordinate system or grid size draft the model reads (an EPSG code is 11 characters).
export const LANDXML_UPLOAD_MAX_DRAFT_CHARS = 64
export const LANDXML_UPLOAD_CRS_HINT = 'Leave blank for none, or enter an EPSG code such as EPSG:2229'
export const LANDXML_UPLOAD_CELLS_HINT
  = `Nodes along the longer side, ${LANDXML_MIN_TARGET_CELLS} to ${LANDXML_MAX_TARGET_CELLS}`

const CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/
const DRAWING_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/
const EPSG_PATTERN = /^EPSG:[1-9][0-9]{0,5}$/
const DIGITS_PATTERN = /^[0-9]+$/
const MAX_PROJECT_ID_CHARS = 100
const MAX_HEAD_INDEX = 4095
const MAX_POINTS = 250_000
const MAX_ABS_BOUND = 1e10
const FILE_UNIT_LABELS = Object.freeze({ meter: 'Meters', foot: 'Feet', USSurveyFoot: 'US survey feet' })
const EXTENT_UNIT_WORDS = Object.freeze({ m: 'meters', ft: 'feet' })

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

function isIntegerIn(value, low, high) {
  return Number.isInteger(value) && value >= low && value <= high
}

// The sentence for one of the control's own reasons or for an upload client code.
export function landxmlUploadSentence(reason) {
  if (typeof reason === 'string' && Object.hasOwn(LANDXML_UPLOAD_REASONS, reason)) return LANDXML_UPLOAD_REASONS[reason]
  return landxmlReason(reason)
}

// The client's crs from a draft: 'none' when blank, else the trimmed draft when it is 'none' or
// an EPSG code the client sends, else null.
export function parseLandxmlCrs(text) {
  if (typeof text !== 'string' || text.length > LANDXML_UPLOAD_MAX_DRAFT_CHARS) return null
  const trimmed = text.trim()
  if (trimmed === '') return 'none'
  return trimmed === 'none' || EPSG_PATTERN.test(trimmed) ? trimmed : null
}

// The grid size from a draft of decimal digits whose value is inside the client's range, else null.
export function parseLandxmlCells(text) {
  if (typeof text !== 'string' || text.length > LANDXML_UPLOAD_MAX_DRAFT_CHARS) return null
  const trimmed = text.trim()
  if (!DIGITS_PATTERN.test(trimmed)) return null
  const cells = Number(trimmed)
  return isIntegerIn(cells, LANDXML_MIN_TARGET_CELLS, LANDXML_MAX_TARGET_CELLS) ? cells : null
}

// The client's own measure of a file: a Blob's size or a byte buffer's length, else null.
function sizeOf(file) {
  const BlobClass = globalThis.Blob
  if (typeof BlobClass === 'function' && file instanceof BlobClass) return file.size
  if (file instanceof Uint8Array || file instanceof ArrayBuffer) return file.byteLength
  return null
}

const refuse = (reason, field) => ({ ok: false, reason, field })

// The request the client will send, or the first refusal in the client's own order.
// `units` is '' until chosen; `crs` and `cells` are the text drafts.
export function buildLandxmlUpload(input) {
  try {
    if (!isPlainObject(input)) return refuse('LANDXML_CLIENT_REQUEST_INVALID', null)
    const { drawingId, projectId = null, file, units, crs, cells } = input
    if (typeof drawingId !== 'string' || !DRAWING_ID_PATTERN.test(drawingId)) {
      return refuse('LANDXML_DRAWING_ID_INVALID', null)
    }
    if (projectId !== null && (typeof projectId !== 'string' || projectId.length === 0
      || projectId.length > 2 * MAX_PROJECT_ID_CHARS || [...projectId].length > MAX_PROJECT_ID_CHARS)) {
      return refuse('LANDXML_PROJECT_ID_INVALID', null)
    }
    if (!LANDXML_DRAWING_UNITS.includes(units)) return refuse('LANDXML_DRAWING_UNITS_INVALID', 'units')
    const parsedCrs = parseLandxmlCrs(crs)
    if (parsedCrs === null) return refuse('LANDXML_CRS_INVALID', 'crs')
    const targetCells = parseLandxmlCells(cells)
    if (targetCells === null) return refuse('LANDXML_TARGET_CELLS_INVALID', 'cells')
    if (file === null || file === undefined) return refuse('file_required', 'file')
    const size = sizeOf(file)
    if (size === null) return refuse('LANDXML_CLIENT_REQUEST_INVALID', 'file')
    if (size === 0) return refuse('LANDXML_EMPTY', 'file')
    if (size > LANDXML_MAX_BYTES) return refuse('LANDXML_TOO_LARGE', 'file')
    if (projectId !== null) {
      try {
        encodeURIComponent(projectId)
      } catch {
        return refuse('LANDXML_PROJECT_ID_INVALID', null)
      }
    }
    return { ok: true, request: { drawingId, file, drawingUnits: units, crs: parsedCrs, targetCells, projectId } }
  } catch {
    return refuse('LANDXML_CLIENT_REQUEST_INVALID', null)
  }
}

function isBound(value) {
  return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= MAX_ABS_BOUND
}

// At most three decimals and no trailing zeros (String(-0) is '0').
function formatBound(value) {
  return String(Number(value.toFixed(3)))
}

function isCrs(value) {
  return value === 'none' || (typeof value === 'string' && EPSG_PATTERN.test(value))
}

// The lines the control shows for a stored terrain, from the client's validated import result,
// or null when a field it would show is missing or outside its range.
export function landxmlUploadSummary(value) {
  try {
    if (!isPlainObject(value) || typeof value.created !== 'boolean') return null
    const { interpretation, points, grid, head } = value
    if (![interpretation, points, grid, head].every(isPlainObject)) return null
    if (!isIntegerIn(head.index, 0, MAX_HEAD_INDEX)) return null
    if (!isIntegerIn(points.declared, 3, MAX_POINTS) || !isIntegerIn(points.accepted, 3, points.declared)) return null
    if (points.skipped !== points.declared - points.accepted) return null
    if (!isIntegerIn(grid.rows, LANDXML_MIN_TARGET_CELLS, LANDXML_MAX_TARGET_CELLS)) return null
    if (!isIntegerIn(grid.cols, LANDXML_MIN_TARGET_CELLS, LANDXML_MAX_TARGET_CELLS)) return null
    if (![grid.x_min, grid.x_max, grid.y_min, grid.y_max].every(isBound)) return null
    if (typeof interpretation.linear_unit !== 'string' || !Object.hasOwn(FILE_UNIT_LABELS, interpretation.linear_unit)) return null
    if (typeof interpretation.drawing_units !== 'string'
      || !Object.hasOwn(EXTENT_UNIT_WORDS, interpretation.drawing_units)) return null
    if (!isCrs(interpretation.crs)) return null
    if (interpretation.crs_source !== 'declared' && interpretation.crs_source !== 'file') return null
    if (interpretation.elevation_datum !== 'unrecorded'
      && !(typeof interpretation.elevation_datum === 'string' && EPSG_PATTERN.test(interpretation.elevation_datum))) return null
    const line = (key, label, text) => Object.freeze({ key, label, text })
    return Object.freeze([
      line('terrain', 'Terrain', value.created
        ? `Stored as terrain change ${head.index + 1} of this drawing`
        : 'Already the terrain of this drawing, so nothing changed'),
      line('points', 'Survey points', points.skipped === 0
        ? `${points.accepted} read`
        : `${points.accepted} of ${points.declared} read, ${points.skipped} skipped`),
      line('grid', 'Terrain grid', `${grid.rows} by ${grid.cols} nodes`),
      line('extent', 'Extent', `X ${formatBound(grid.x_min)} to ${formatBound(grid.x_max)}, Y ${formatBound(grid.y_min)} to ${formatBound(grid.y_max)} ${EXTENT_UNIT_WORDS[interpretation.drawing_units]}`),
      line('file_units', 'File units', FILE_UNIT_LABELS[interpretation.linear_unit]),
      line('crs', 'Coordinate system', interpretation.crs === 'none' ? 'None'
        : interpretation.crs_source === 'file' ? `${interpretation.crs}, named by the file`
          : `${interpretation.crs}, as chosen`),
      line('datum', 'Elevation datum', interpretation.elevation_datum === 'unrecorded'
        ? 'Not recorded' : interpretation.elevation_datum),
    ])
  } catch {
    return null
  }
}

const refusedOutcome = (code, retryable) => Object.freeze({
  kind: 'refused', code, retryable, text: landxmlUploadSentence(code),
})

// What the control shows for the client's answer: { kind: 'imported', value, lines } or
// { kind: 'refused', code, retryable, text }. Anything that is neither a success with a
// readable summary nor a coded refusal is LANDXML_CLIENT_RESPONSE_INVALID.
export function landxmlUploadOutcome(result) {
  try {
    if (isPlainObject(result) && result.ok === true && result.status === 200) {
      const lines = landxmlUploadSummary(result.value)
      if (lines !== null) return Object.freeze({ kind: 'imported', value: result.value, lines })
    } else if (isPlainObject(result) && result.ok === false
      && typeof result.code === 'string' && CODE_PATTERN.test(result.code)) {
      return refusedOutcome(result.code, result.retryable === true)
    }
  } catch {
    // Falls through to the closed refusal.
  }
  return refusedOutcome('LANDXML_CLIENT_RESPONSE_INVALID', false)
}

// The refusal shown when the person cancels an import from the control.
export function landxmlUploadCancelled() {
  return refusedOutcome('upload_cancelled', false)
}
