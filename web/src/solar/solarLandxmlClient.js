// The Studio side of the LandXML terrain upload route. Pure transport seam: no DOM, no storage,
// no api.js import; every dependency is injected. The one call is bounded (request size, response
// size, wall time) and never rejects: it resolves to { ok: true, status: 200, value } or
// { ok: false, status, code, retryable }. Fails closed: a success body that is not exactly the
// documented shape, or that does not describe the request it answers, is
// LANDXML_CLIENT_RESPONSE_INVALID.
//
// Route (server/routers/drawings.py): POST /api/drawings/{drawing_id}/imports/landxml with the
// file's bytes as the body (application/xml) and drawing_units, crs, target_cells and an optional
// project_id in the query. Every refusal body carries its code, never prose, so this module owns
// the sentence for each key of the route's LANDXML_IMPORT_REFUSALS.
//
// Wall time: the call has one deadline, LANDXML_TIMEOUT_MS unless the client was created with
// another, measured from the call's start. It covers the request, the headers and every body
// read, so a server that sends headers and then stalls its body still settles the call. The
// response body is bounded in bytes while reading, never after.
import { FetchTimeoutError, fetchWithBudget } from '../fetchBudget.js'

export const LANDXML_MAX_BYTES = 16_777_216
export const LANDXML_RESPONSE_MAX_BYTES = 65_536
export const LANDXML_DEFAULT_TARGET_CELLS = 30
export const LANDXML_MIN_TARGET_CELLS = 2
export const LANDXML_MAX_TARGET_CELLS = 200
export const LANDXML_DRAWING_UNITS = Object.freeze(['m', 'ft'])
export const LANDXML_TIMEOUT_MS = 120_000
export const LANDXML_IMPORT_FALLBACK = 'The LandXML import stopped'

export const LANDXML_IMPORT_REASONS = Object.freeze({
  LANDXML_DRAWING_ID_INVALID: 'This drawing cannot take a LandXML file because its id is not valid',
  LANDXML_PROJECT_ID_INVALID: 'The project named for this import is not valid',
  LANDXML_DRAWING_UNITS_INVALID: 'Choose meters or feet as the drawing units before importing',
  LANDXML_CRS_INVALID: 'Choose no coordinate system or an EPSG code such as EPSG:2229',
  LANDXML_TARGET_CELLS_INVALID: 'Choose a grid size from 2 to 200 cells',
  LANDXML_MEDIA_TYPE_REFUSED: 'The file was not sent as XML, so choose the LandXML file again',
  LANDXML_EMPTY: 'The chosen file is empty, so choose the LandXML file again',
  LANDXML_TOO_LARGE: 'The file is larger than 16 MiB, more than an import accepts',
  LANDXML_ENCODING_INVALID: 'The file is not UTF-8 text as its XML declaration says, so export it again',
  LANDXML_UNSAFE: 'The file declares a document type or entities, which an import never reads',
  LANDXML_MALFORMED: 'The file is not well formed XML, so export it again',
  LANDXML_NOT_LANDXML: 'The file is XML but not LandXML, so choose the LandXML export',
  LANDXML_UNITS_MISSING: 'The file does not say its units, so export it with its Units element',
  LANDXML_UNITS_UNSUPPORTED: 'The file uses units an import cannot read, only meters, feet or US survey feet',
  LANDXML_CRS_UNSUPPORTED: 'The file names its coordinate system without an EPSG code',
  LANDXML_TOO_MANY_POINTS: 'The file has more than 250,000 points, more than an import accepts',
  LANDXML_TOO_FEW_POINTS: 'The file has fewer than 3 survey points, too few for a terrain grid',
  LANDXML_COORDINATE_OUT_OF_RANGE: 'A point in the file is too far from the origin to import',
  LANDXML_SOURCE_ID_INVALID: 'The stored LandXML reference is not valid, so import the file again',
  LANDXML_DRAWING_NOT_FOUND: 'This drawing could not be found, so reopen it and try again',
  LANDXML_SOURCE_NOT_FOUND: 'The stored LandXML file was not found for this drawing, so import it again',
  LANDXML_GRAPH_REQUIRED: 'Start the solar design with Solar settings before importing terrain',
  LANDXML_PROJECT_MISMATCH: 'This drawing belongs to another project than the one named for the import',
  LANDXML_CRS_MISMATCH: 'The coordinate system does not match the file or the terrain already in this drawing',
  LANDXML_UNITS_MISMATCH: 'The drawing units do not match the terrain already in this drawing',
  LANDXML_SOURCE_KIND_MISMATCH: 'The stored file is not a LandXML source, so import the file again',
  LANDXML_RESAMPLE_TOO_LARGE: 'This file and grid size need too much work, so choose fewer cells',
  LANDXML_WRITES_DRAINED: 'Drawing changes are paused right now, so try the import again shortly',
  LANDXML_STORE_UNAVAILABLE: 'The drawing store is unavailable right now, so try again shortly',
  LANDXML_SOURCE_CORRUPT: 'The stored LandXML file failed its integrity check, so import it again',
  LANDXML_IMPORT_FAILED: 'The terrain could not be imported, so nothing was saved',
  PHYSICAL_HEAD_CONFLICT: 'Another change reached this drawing first, so try the import again',
  PHYSICAL_HEAD_LOG_FULL: 'This drawing holds the most terrain changes it can keep',
  PHYSICAL_HEAD_PROJECT_MISMATCH: 'The terrain in this drawing belongs to another project',
  PHYSICAL_STATE_PROJECT_MISMATCH: 'The terrain record belongs to another project than this drawing',
  PHYSICAL_HEAD_WRITES_DRAINED: 'Drawing changes are paused right now, so try the import again shortly',
  PHYSICAL_STATE_WRITES_DRAINED: 'Drawing changes are paused right now, so try the import again shortly',
  PHYSICAL_HEAD_STORE_UNAVAILABLE: 'The drawing store is unavailable right now, so try again shortly',
  PHYSICAL_STATE_STORE_UNAVAILABLE: 'The drawing store is unavailable right now, so try again shortly',
  PHYSICAL_HEAD_CORRUPT: 'The terrain history of this drawing failed its integrity check',
  PHYSICAL_HEAD_STORE_UNSAFE: 'This drawing store cannot record terrain changes safely',
  PHYSICAL_STATE_CORRUPT: 'The stored terrain failed its integrity check',
  UNAUTHENTICATED: 'Sign in again to import a LandXML file',
  FORBIDDEN: 'A guest session cannot import terrain, so sign in to an account',
  ENTITLEMENT_REQUIRED: 'Your plan does not include uploads, so this import stays off',
  ENTITLEMENT_POLICY_UNAVAILABLE: 'The plan policy could not be read, so this step stays off',
  LANDXML_CLIENT_REQUEST_INVALID: 'This LandXML step was given input it cannot send',
  LANDXML_CLIENT_TIMEOUT: 'The server did not answer in time, so try again',
  LANDXML_CLIENT_NETWORK: 'The server could not be reached, so try again',
  LANDXML_CLIENT_ABORTED: 'This LandXML step was cancelled',
  LANDXML_CLIENT_RESPONSE_INVALID: 'The server answer could not be read, so the step stopped',
})

const CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/
const DRAWING_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/
const EPSG_PATTERN = /^EPSG:[1-9][0-9]{0,5}$/
const HEX64_PATTERN = /^[0-9a-f]{64}$/
const MAX_PROJECT_ID_CHARS = 100
const MAX_SOURCE_VERSION = 2147483647
const MAX_HEAD_INDEX = 4095
const MAX_POINTS = 250_000
const MAX_ABS_BOUND = 1e10
const MAX_API_BASE_LENGTH = 2048
const MAX_TIMEOUT_MS = 600_000
// Metres per drawing unit (solar_physical_state.UNITS) and per source unit
// (solar_landxml_import.LINEAR_UNITS), the only values the route can answer.
const METERS_PER_DRAWING_UNIT = Object.freeze({ m: 1.0, ft: 0.3048 })
const METERS_PER_SOURCE_UNIT = Object.freeze({ meter: 1.0, foot: 0.3048, USSurveyFoot: 1200.0 / 3937.0 })
const RESULT_KEYS = Object.freeze([
  'schema', 'created', 'drawing_id', 'project_id', 'source', 'interpretation', 'points', 'grid', 'head',
])
const ENVELOPE_KEYS = Object.freeze(['error', 'degraded_mode'])
const REF_KEYS = Object.freeze([
  'schema', 'artifact_id', 'media_type', 'filename', 'byte_length', 'content_sha256', 'source_version', 'download',
])
const INTERPRETATION_KEYS = Object.freeze([
  'point_order', 'drawing_x', 'drawing_y', 'linear_unit', 'meters_per_source_unit', 'drawing_units',
  'meters_per_drawing_unit', 'horizontal_scale', 'elevation_scale', 'crs', 'crs_source', 'elevation_datum',
])
const POINT_KEYS = Object.freeze(['declared', 'accepted', 'skipped'])
const GRID_KEYS = Object.freeze(['rows', 'cols', 'target_cells', 'x_min', 'x_max', 'y_min', 'y_max'])
const HEAD_KEYS = Object.freeze(['schema', 'drawing_id', 'project_id', 'index', 'parent', 'state'])

// Resolves to the sentence for a code; never echoes a malformed or oversized value.
export function landxmlReason(code) {
  if (typeof code === 'string' && Object.hasOwn(LANDXML_IMPORT_REASONS, code)) return LANDXML_IMPORT_REASONS[code]
  if (typeof code === 'string' && CODE_PATTERN.test(code)) return `${LANDXML_IMPORT_FALLBACK} (${code})`
  return LANDXML_IMPORT_FALLBACK
}

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

// 1 to 100 code points, the route's own bound (Python len counts code points).
function isProjectId(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 2 * MAX_PROJECT_ID_CHARS) return false
  return [...value].length <= MAX_PROJECT_ID_CHARS
}

function isDrawingId(value) {
  return typeof value === 'string' && DRAWING_ID_PATTERN.test(value)
}

function isCrs(value) {
  return typeof value === 'string' && (value === 'none' || EPSG_PATTERN.test(value))
}

function isBound(value) {
  return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= MAX_ABS_BOUND
}

function copyOf(value, keys) {
  const copy = {}
  for (const key of keys) copy[key] = value[key]
  return copy
}

function validRef(ref, drawingId, mediaType, filename) {
  if (!isPlainObject(ref) || !hasExactKeys(ref, REF_KEYS)) return false
  if (ref.schema !== 'leaf.solar-artifact-ref.v1') return false
  if (typeof ref.artifact_id !== 'string' || !HEX64_PATTERN.test(ref.artifact_id)) return false
  if (typeof ref.content_sha256 !== 'string' || !HEX64_PATTERN.test(ref.content_sha256)) return false
  if (ref.media_type !== mediaType || ref.filename !== filename) return false
  if (!isIntegerIn(ref.byte_length, 1, LANDXML_MAX_BYTES)) return false
  if (!isIntegerIn(ref.source_version, 1, MAX_SOURCE_VERSION)) return false
  return ref.download === `/api/drawings/${drawingId}/artifacts/${ref.artifact_id}`
}

function validInterpretation(value, request) {
  if (!isPlainObject(value) || !hasExactKeys(value, INTERPRETATION_KEYS)) return false
  if (value.point_order !== 'northing-easting-elevation') return false
  if (value.drawing_x !== 'easting' || value.drawing_y !== 'northing') return false
  if (typeof value.linear_unit !== 'string' || !Object.hasOwn(METERS_PER_SOURCE_UNIT, value.linear_unit)) return false
  const perSource = METERS_PER_SOURCE_UNIT[value.linear_unit]
  const perDrawing = METERS_PER_DRAWING_UNIT[request.drawingUnits]
  if (value.meters_per_source_unit !== perSource) return false
  if (value.drawing_units !== request.drawingUnits || value.meters_per_drawing_unit !== perDrawing) return false
  if (value.horizontal_scale !== perSource / perDrawing || value.elevation_scale !== perSource) return false
  if (value.crs !== request.crs) return false
  if (value.crs_source !== 'declared' && !(value.crs_source === 'file' && value.crs !== 'none')) return false
  return value.elevation_datum === 'unrecorded'
    || (typeof value.elevation_datum === 'string' && EPSG_PATTERN.test(value.elevation_datum))
}

function validPoints(value) {
  if (!isPlainObject(value) || !hasExactKeys(value, POINT_KEYS)) return false
  if (!isIntegerIn(value.declared, 3, MAX_POINTS) || !isIntegerIn(value.accepted, 3, value.declared)) return false
  return value.skipped === value.declared - value.accepted
}

function validGrid(value, targetCells) {
  if (!isPlainObject(value) || !hasExactKeys(value, GRID_KEYS)) return false
  if (value.target_cells !== targetCells) return false
  if (!isIntegerIn(value.rows, LANDXML_MIN_TARGET_CELLS, LANDXML_MAX_TARGET_CELLS)) return false
  if (!isIntegerIn(value.cols, LANDXML_MIN_TARGET_CELLS, LANDXML_MAX_TARGET_CELLS)) return false
  // grid_dimensions: the longer axis takes exactly target_cells nodes.
  if (Math.max(value.rows, value.cols) !== targetCells) return false
  if (![value.x_min, value.x_max, value.y_min, value.y_max].every(isBound)) return false
  return value.x_min < value.x_max && value.y_min < value.y_max
}

function validHead(value, drawingId, projectId) {
  if (!isPlainObject(value) || !hasExactKeys(value, HEAD_KEYS)) return false
  if (value.schema !== 'leaf.solar-physical-head.v1') return false
  if (value.drawing_id !== drawingId || value.project_id !== projectId) return false
  if (!isIntegerIn(value.index, 0, MAX_HEAD_INDEX)) return false
  if (value.index === 0 ? value.parent !== null
    : (typeof value.parent !== 'string' || !HEX64_PATTERN.test(value.parent))) return false
  return validRef(value.state, drawingId, 'application/json', 'physical-state.json')
}

// The normalized import result (9 keys, envelope fields dropped) or null. `request` is the
// request it answers: { drawingId, drawingUnits, crs, targetCells, projectId, byteLength }.
export function validateLandxmlImport(body, request) {
  if (!isPlainObject(request)) return null
  const { drawingId, drawingUnits, crs, targetCells, projectId = null, byteLength } = request
  if (!isDrawingId(drawingId) || !LANDXML_DRAWING_UNITS.includes(drawingUnits) || !isCrs(crs)) return null
  if (!isIntegerIn(targetCells, LANDXML_MIN_TARGET_CELLS, LANDXML_MAX_TARGET_CELLS)) return null
  if (projectId !== null && !isProjectId(projectId)) return null
  if (!isIntegerIn(byteLength, 1, LANDXML_MAX_BYTES)) return null
  if (!isPlainObject(body) || !hasExactKeys(body, RESULT_KEYS, ENVELOPE_KEYS)) return null
  if (Object.hasOwn(body, 'error') && body.error !== null) return null
  if (Object.hasOwn(body, 'degraded_mode') && typeof body.degraded_mode !== 'boolean') return null
  if (body.schema !== 'leaf.solar-landxml-import.v1' || typeof body.created !== 'boolean') return null
  if (body.drawing_id !== drawingId || !isProjectId(body.project_id)) return null
  if (projectId !== null && body.project_id !== projectId) return null
  if (!validRef(body.source, drawingId, 'application/xml', 'landxml-source.xml')) return null
  // The route stores the uploaded bytes exactly, so the source is as long as the file sent.
  if (body.source.byte_length !== byteLength) return null
  if (!validInterpretation(body.interpretation, { drawingUnits, crs })) return null
  if (!validPoints(body.points) || !validGrid(body.grid, targetCells)) return null
  if (!validHead(body.head, drawingId, body.project_id)) return null
  return {
    schema: body.schema,
    created: body.created,
    drawing_id: body.drawing_id,
    project_id: body.project_id,
    source: copyOf(body.source, REF_KEYS),
    interpretation: copyOf(body.interpretation, INTERPRETATION_KEYS),
    points: copyOf(body.points, POINT_KEYS),
    grid: copyOf(body.grid, GRID_KEYS),
    head: { ...copyOf(body.head, HEAD_KEYS), state: copyOf(body.head.state, REF_KEYS) },
  }
}

function refuse(status, code, retryable) {
  return { ok: false, status, code, retryable }
}

function headerOf(response, name) {
  try {
    const value = response?.headers?.get?.(name)
    return typeof value === 'string' ? value : null
  } catch {
    return null
  }
}

// True when a present all-digit content-length is over the cap.
function declaredOver(response, cap) {
  const value = headerOf(response, 'content-length')
  if (value === null) return false
  const digits = value.trim().replace(/^0+(?=\d)/, '')
  if (!/^\d+$/.test(digits)) return false
  return digits.length > 16 || Number(digits) > cap
}

const UNREADABLE = Object.freeze({ ok: false })
const STOPPED = Object.freeze({ stopped: true })

// Releases a body the call will not read to its end. A throwing or rejecting cancel never
// changes the result.
function releaseBody(response, reader = null) {
  try {
    const pending = reader !== null ? reader.cancel() : response?.body?.cancel?.()
    if (pending && typeof pending.then === 'function') pending.then(undefined, () => {})
  } catch {
    // Nothing left to release.
  }
}

function monotonicNow() {
  return typeof globalThis.performance?.now === 'function' ? globalThis.performance.now() : Date.now()
}

// One call's deadline and cancellation (the solarImportClient.js design): the timer ends a
// stalled read, and expired() ends a flood of reads that are always ready.
function openCall(timeoutMs, callerSignal, startedAt) {
  const controller = new AbortController()
  let reason = null
  let wake
  const stopped = new Promise((resolve) => { wake = resolve })
  const halt = (why) => {
    if (reason !== null) return
    reason = why
    wake(STOPPED)
    controller.abort()
  }
  const onCallerAbort = () => halt('aborted')
  const timer = setTimeout(() => halt('timeout'), Math.max(0, timeoutMs - (monotonicNow() - startedAt)))
  if (callerSignal) {
    // A signal whose removeEventListener throws keeps its listener; the client cannot remove it.
    try {
      if (callerSignal.aborted) onCallerAbort()
      else callerSignal.addEventListener('abort', onCallerAbort, { once: true })
    } catch (error) {
      clearTimeout(timer)
      try {
        callerSignal.removeEventListener('abort', onCallerAbort)
      } catch {
        // Cleanup must not replace the registration error.
      }
      throw error
    }
  }
  return {
    signal: controller.signal,
    timeoutMs,
    halt,
    reason: () => reason,
    expired() {
      if (reason !== null) return true
      if (monotonicNow() - startedAt < timeoutMs) return false
      halt('timeout')
      return true
    },
    race(work) {
      return Promise.race([
        Promise.resolve().then(work).then((value) => ({ value }), (error) => ({ error })),
        stopped,
      ])
    },
    failure(status) {
      return reason === 'aborted'
        ? refuse(status, 'LANDXML_CLIENT_ABORTED', false)
        : refuse(status, 'LANDXML_CLIENT_TIMEOUT', true)
    },
    close() {
      clearTimeout(timer)
      try {
        callerSignal?.removeEventListener('abort', onCallerAbort)
      } catch {
        // A cleanup failure never changes the result.
      }
    },
  }
}

function isByteChunk(chunk) {
  return Object.prototype.toString.call(chunk) === '[object Uint8Array]'
}

// Bounded JSON read: refuses an oversized declared or actual body (in bytes) and malformed
// UTF-8. Resolves to STOPPED, UNREADABLE or { ok: true, value }.
async function readBoundedJson(call, response) {
  if (declaredOver(response, LANDXML_RESPONSE_MAX_BYTES)) {
    releaseBody(response)
    return UNREADABLE
  }
  let body
  try {
    body = response.body
  } catch {
    return UNREADABLE
  }
  let text
  if (body !== null && body !== undefined) {
    if (typeof body.getReader !== 'function') {
      releaseBody(response)
      return UNREADABLE
    }
    let reader
    try {
      reader = body.getReader()
    } catch {
      return UNREADABLE
    }
    const chunks = []
    let total = 0
    for (;;) {
      if (call.expired()) {
        releaseBody(null, reader)
        return STOPPED
      }
      const outcome = await call.race(() => reader.read())
      if (outcome === STOPPED) {
        releaseBody(null, reader)
        return STOPPED
      }
      const step = 'error' in outcome ? null : outcome.value
      if (step === null || typeof step !== 'object') {
        releaseBody(null, reader)
        return UNREADABLE
      }
      if (step.done === true) {
        if (call.expired()) {
          releaseBody(null, reader)
          return STOPPED
        }
        break
      }
      if (!isByteChunk(step.value)) {
        releaseBody(null, reader)
        return UNREADABLE
      }
      total += step.value.byteLength
      if (total > LANDXML_RESPONSE_MAX_BYTES) {
        releaseBody(null, reader)
        return UNREADABLE
      }
      chunks.push(step.value)
    }
    const bytes = new Uint8Array(total)
    let offset = 0
    for (const chunk of chunks) {
      bytes.set(chunk, offset)
      offset += chunk.byteLength
    }
    try {
      text = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
    } catch {
      return UNREADABLE
    }
  } else {
    if (typeof response.text !== 'function') return UNREADABLE
    const outcome = await call.race(() => response.text())
    if (outcome === STOPPED) return STOPPED
    if ('error' in outcome || typeof outcome.value !== 'string') return UNREADABLE
    if (call.expired()) return STOPPED
    text = outcome.value
    // Every UTF-16 unit is at least one UTF-8 byte, so the length alone can refuse without encoding.
    if (text.length > LANDXML_RESPONSE_MAX_BYTES
      || new TextEncoder().encode(text).byteLength > LANDXML_RESPONSE_MAX_BYTES) return UNREADABLE
    // A replaced invalid byte and literal U+FFFD are indistinguishable once decoded, so fail closed here as route envelopes never carry it.
    if (text.includes('\uFFFD')) return UNREADABLE
  }
  try {
    return { ok: true, value: JSON.parse(text) }
  } catch {
    return UNREADABLE
  }
}

// Maps a non-200, non-401 answer to a failure result.
async function refusalOf(call, response, status) {
  const retryableServer = status >= 500
  if (status >= 200 && status < 300) {
    releaseBody(response)
    return refuse(status, 'LANDXML_CLIENT_RESPONSE_INVALID', false)
  }
  const read = await readBoundedJson(call, response)
  if (read === STOPPED) return call.failure(status)
  if (!read.ok) return refuse(status, 'LANDXML_CLIENT_RESPONSE_INVALID', retryableServer)
  const body = read.value
  if (isPlainObject(body) && body.entitlement_required === true) {
    if (status === 403) return refuse(status, 'ENTITLEMENT_REQUIRED', false)
    if (status === 503) return refuse(status, 'ENTITLEMENT_POLICY_UNAVAILABLE', true)
    return refuse(status, 'LANDXML_CLIENT_RESPONSE_INVALID', retryableServer)
  }
  if (isPlainObject(body) && isPlainObject(body.error)
    && typeof body.error.reason_code === 'string' && CODE_PATTERN.test(body.error.reason_code)) {
    return refuse(status, body.error.reason_code, body.error.retryable === true)
  }
  // The guest gate (server/deps.py) answers 403 FORBIDDEN with no reason code.
  if (status === 403 && isPlainObject(body) && isPlainObject(body.error) && body.error.error_code === 'FORBIDDEN') {
    return refuse(status, 'FORBIDDEN', false)
  }
  return refuse(status, 'LANDXML_CLIENT_RESPONSE_INVALID', retryableServer)
}

function isSignal(signal) {
  return signal === undefined || signal === null
    || (typeof signal === 'object' && typeof signal.aborted === 'boolean'
      && typeof signal.addEventListener === 'function' && typeof signal.removeEventListener === 'function')
}

function sizeOf(file) {
  const BlobClass = globalThis.Blob
  if (typeof BlobClass === 'function' && file instanceof BlobClass) return file.size
  if (file instanceof Uint8Array || file instanceof ArrayBuffer) return file.byteLength
  return null
}

export function createSolarLandxmlClient({ fetchImpl, apiBase = '', headers, onResponse, timeoutMs } = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('fetchImpl must be a function')
  if (typeof headers !== 'function') throw new TypeError('headers must be a function')
  if (typeof apiBase !== 'string' || apiBase.length > MAX_API_BASE_LENGTH) {
    throw new TypeError('apiBase must be a string of at most 2048 characters')
  }
  if (onResponse !== undefined && typeof onResponse !== 'function') throw new TypeError('onResponse must be a function')
  if (timeoutMs !== undefined && !isIntegerIn(timeoutMs, 1, MAX_TIMEOUT_MS)) {
    throw new TypeError('timeoutMs must be an integer from 1 to 600000 ms')
  }
  const budget = timeoutMs ?? LANDXML_TIMEOUT_MS

  function requestHeaders(drawingId) {
    let value
    try {
      value = headers(drawingId)
    } catch {
      return null
    }
    if (!isPlainObject(value)) return null
    const copy = Object.create(null)
    for (const key of Reflect.ownKeys(value)) {
      if (typeof key !== 'string' || typeof value[key] !== 'string') return null
      copy[key] = value[key]
    }
    return copy
  }

  async function exchange(call, { drawingId, url, file, signal }) {
    const base = requestHeaders(drawingId)
    if (base === null) return { failure: refuse(null, 'LANDXML_CLIENT_REQUEST_INVALID', false) }
    const sent = Object.create(null)
    for (const key of Object.keys(base)) {
      if (key.toLowerCase() !== 'content-type') sent[key] = base[key]
    }
    sent['Content-Type'] = 'application/xml'
    const init = { method: 'POST', headers: sent, body: file, signal: call.signal }
    const callFetch = async (input, budgetInit) => {
      budgetInit?.signal?.addEventListener?.('abort', () => call.halt('timeout'), { once: true })
      const raw = Promise.resolve().then(() => fetchImpl(input, { ...budgetInit, signal: call.signal }))
      const settled = await call.race(() => raw)
      if (settled === STOPPED) {
        // The helper's finally must run now, so its timer and listener are released with the call.
        raw.then((late) => releaseBody(late), () => {})
        throw new Error('landxml call stopped')
      }
      if ('error' in settled) throw settled.error
      return settled.value
    }
    const pending = Promise.resolve().then(() => fetchWithBudget(callFetch, url, init, call.timeoutMs))
    const outcome = await call.race(() => pending)
    if (outcome === STOPPED) {
      pending.then((late) => releaseBody(late), () => {})
      return { failure: call.failure(null) }
    }
    if ('error' in outcome) {
      if (outcome.error instanceof FetchTimeoutError || call.reason() === 'timeout') {
        return { failure: refuse(null, 'LANDXML_CLIENT_TIMEOUT', true) }
      }
      if (call.reason() === 'aborted' || signal?.aborted === true) {
        return { failure: refuse(null, 'LANDXML_CLIENT_ABORTED', false) }
      }
      return { failure: refuse(null, 'LANDXML_CLIENT_NETWORK', true) }
    }
    const response = outcome.value
    if (onResponse) {
      const authorizationKey = Object.keys(sent).find((key) => key.toLowerCase() === 'authorization')
      try {
        onResponse(response, url, authorizationKey === undefined ? undefined : sent[authorizationKey])
      } catch {
        // The observer's failure never changes the result.
      }
    }
    const status = response?.status
    if (!Number.isInteger(status)) {
      releaseBody(response)
      return { failure: refuse(null, 'LANDXML_CLIENT_RESPONSE_INVALID', false) }
    }
    if (status === 401) {
      releaseBody(response)
      return { failure: refuse(401, 'UNAUTHENTICATED', false) }
    }
    if (status !== 200) return { failure: await refusalOf(call, response, status) }
    return { response }
  }

  async function uploadLandxml(options) {
    try {
      const startedAt = monotonicNow()
      if (options === undefined) options = {}
      if (options === null || typeof options !== 'object') {
        return refuse(null, 'LANDXML_CLIENT_REQUEST_INVALID', false)
      }
      const {
        drawingId, file, drawingUnits, crs, targetCells = LANDXML_DEFAULT_TARGET_CELLS, projectId = null, signal,
      } = options
      if (!isDrawingId(drawingId)) return refuse(null, 'LANDXML_DRAWING_ID_INVALID', false)
      if (projectId !== null && !isProjectId(projectId)) return refuse(null, 'LANDXML_PROJECT_ID_INVALID', false)
      if (!LANDXML_DRAWING_UNITS.includes(drawingUnits)) return refuse(null, 'LANDXML_DRAWING_UNITS_INVALID', false)
      if (!isCrs(crs)) return refuse(null, 'LANDXML_CRS_INVALID', false)
      if (!isIntegerIn(targetCells, LANDXML_MIN_TARGET_CELLS, LANDXML_MAX_TARGET_CELLS)) {
        return refuse(null, 'LANDXML_TARGET_CELLS_INVALID', false)
      }
      const size = sizeOf(file)
      if (size === null) return refuse(null, 'LANDXML_CLIENT_REQUEST_INVALID', false)
      if (size === 0) return refuse(null, 'LANDXML_EMPTY', false)
      if (size > LANDXML_MAX_BYTES) return refuse(null, 'LANDXML_TOO_LARGE', false)
      if (!isSignal(signal)) return refuse(null, 'LANDXML_CLIENT_REQUEST_INVALID', false)
      let query = `?drawing_units=${drawingUnits}&crs=${encodeURIComponent(crs)}&target_cells=${targetCells}`
      if (projectId !== null) {
        try {
          query += `&project_id=${encodeURIComponent(projectId)}`
        } catch {
          return refuse(null, 'LANDXML_PROJECT_ID_INVALID', false)
        }
      }
      const url = `${apiBase}/api/drawings/${encodeURIComponent(drawingId)}/imports/landxml${query}`
      const request = { drawingId, drawingUnits, crs, targetCells, projectId, byteLength: size }
      const call = openCall(budget, signal, startedAt)
      try {
        if (call.expired()) return call.failure(null)
        const exchanged = await exchange(call, { drawingId, url, file, signal })
        if (exchanged.failure) return exchanged.failure
        const read = await readBoundedJson(call, exchanged.response)
        if (read === STOPPED) return call.failure(200)
        const value = read.ok ? validateLandxmlImport(read.value, request) : null
        return value === null
          ? refuse(200, 'LANDXML_CLIENT_RESPONSE_INVALID', false)
          : { ok: true, status: 200, value }
      } finally {
        call.close()
      }
    } catch {
      return refuse(null, 'LANDXML_CLIENT_REQUEST_INVALID', false)
    }
  }

  return Object.freeze({ uploadLandxml })
}
