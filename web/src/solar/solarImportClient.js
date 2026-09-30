// The Studio side of the SolarEdge import routes. Pure transport seam: no DOM,
// no storage, no api.js import; every dependency is injected. Every call is
// bounded (request size, response size, wall time) and never rejects: it
// resolves to { ok: true, status: 200, value } or
// { ok: false, status, code, retryable }. Fails closed: a success body that is
// not exactly the documented shape is SOLAREDGE_CLIENT_RESPONSE_INVALID, and a
// download whose bytes do not match their record is refused.
//
// Routes (server/routers/drawings.py): POST .../imports/solaredge-pdf (the PDF
// bytes), POST .../imports/solaredge-pdf/report (a JSON match request of at
// most 4096 bytes) and GET .../artifacts/{artifact_id}. Every refusal body
// carries its code, never prose, so this module owns the sentence for each.
//
// Wall time: each call has one deadline, its budget from SOLAREDGE_TIMEOUTS_MS,
// measured from the call's start. It covers the request, the headers and every
// body read, so a server that sends headers and then stalls its body still
// settles the call. Body size is bounded in bytes while reading, never after.
//
// A download is kept only after its SHA-256 matches the record. The default
// digest is crypto.subtle, present in Node and in browsers on a secure origin
// (https or localhost). On a plain http LAN address it is missing, so the
// download answers SOLAREDGE_CLIENT_DIGEST_UNAVAILABLE and keeps nothing: that
// is deliberate fail-closed behaviour, not a gap.
import { FetchTimeoutError, fetchWithBudget } from '../fetchBudget.js'

export const SOLAREDGE_PDF_MAX_BYTES = 16_777_216
export const SOLAREDGE_REPORT_REQUEST_MAX_BYTES = 4096
export const SOLAR_ARTIFACT_MAX_BYTES = 16_777_216
export const SOLAREDGE_RESPONSE_MAX_BYTES = 65_536
export const SOLAREDGE_MIN_ALIGNMENT_TOLERANCE = 1e-6
export const SOLAREDGE_MAX_ALIGNMENT_TOLERANCE = 1e6
export const SOLAREDGE_SELECTION_ORDERS = Object.freeze(['unknown', 'recorded'])
export const SOLAREDGE_TIMEOUTS_MS = Object.freeze({ upload: 120_000, report: 120_000, artifact: 60_000 })
export const SOLAREDGE_IMPORT_FALLBACK = 'The SolarEdge import stopped'

export const SOLAREDGE_IMPORT_REASONS = Object.freeze({
  IMPORT_DRAWING_ID_INVALID: 'This drawing cannot take a SolarEdge PDF because its id is not valid',
  IMPORT_PROJECT_ID_INVALID: 'The project named for this import is not valid',
  IMPORT_MEDIA_TYPE_REFUSED: 'Choose a PDF file exported from SolarEdge Designer',
  IMPORT_PDF_EMPTY: 'The chosen file is empty, so choose the SolarEdge PDF again',
  IMPORT_NOT_A_PDF: 'The chosen file is not a PDF, so choose the SolarEdge Designer PDF',
  IMPORT_PDF_MALFORMED: 'The PDF could not be read, so export it from SolarEdge Designer again',
  IMPORT_PDF_ENCRYPTED: 'The PDF is password protected, so export a copy without a password',
  IMPORT_PDF_NO_PAGES: 'The PDF has no pages, so export it from SolarEdge Designer again',
  IMPORT_PDF_TOO_MANY_PAGES: 'The PDF has more than 50 pages, more than an import accepts',
  IMPORT_PDF_TOO_LARGE: 'The PDF is larger than 16 MiB, more than an import accepts',
  IMPORT_DRAWING_NOT_FOUND: 'This drawing could not be found, so reopen it and try again',
  IMPORT_GRAPH_REQUIRED: 'Start the solar design with Solar settings before importing a PDF',
  IMPORT_PROJECT_MISMATCH: 'This drawing belongs to another project than the one named for the import',
  IMPORT_QUOTA_EXCEEDED: 'This drawing already holds 8 SolarEdge PDFs, the most it can keep',
  IMPORT_WRITES_DRAINED: 'Drawing changes are paused right now, so try the import again shortly',
  IMPORT_STORE_UNAVAILABLE: 'The drawing store is unavailable right now, so try again shortly',
  IMPORT_SOURCE_CONFLICT: 'A stored PDF does not match this upload, so the import stopped',
  IMPORT_SOURCE_INVALID: 'The PDF could not be stored, so the import stopped',
  IMPORT_SOURCE_ID_INVALID: 'The uploaded PDF reference is not valid, so upload the PDF again',
  IMPORT_SOURCE_NOT_FOUND: 'The uploaded PDF was not found for this drawing, so upload it again',
  IMPORT_SOURCE_KIND_MISMATCH: 'The chosen upload is not a SolarEdge PDF, so upload the PDF again',
  IMPORT_SOURCE_CORRUPT: 'The stored PDF failed its integrity check, so upload it again',
  REPORT_DRAWING_ID_INVALID: 'This drawing cannot be matched because its id is not valid',
  REPORT_REQUEST_INVALID: 'The match request is not valid, so check the alignment tolerance',
  REPORT_MEDIA_TYPE_REFUSED: 'The match request was not sent as JSON, so the report stopped',
  REPORT_REQUEST_TOO_LARGE: 'The match request is larger than 4096 bytes, so the report stopped',
  REPORT_DRAWING_NOT_FOUND: 'This drawing could not be found, so reopen it and try again',
  REPORT_GRAPH_REQUIRED: 'Start the solar design with Solar settings before matching a PDF',
  REPORT_PROJECT_MISMATCH: 'This drawing belongs to another project than the one named for the report',
  REPORT_UNITS_UNRESOLVED: 'Set the drawing units in Solar settings before matching the PDF',
  REPORT_FRAMES_REQUIRED: 'Create panel groups before matching the PDF',
  REPORT_FRAME_EMPTY: 'Every panel group needs panels before the PDF can be matched',
  REPORT_PANEL_HANDLE_INVALID: 'Some panels have no drawing handle, so the PDF cannot be matched',
  REPORT_PANEL_HANDLE_DUPLICATE: 'Two panels share one drawing handle, so the PDF cannot be matched',
  REPORT_AMBIGUOUS_MATCH: 'A panel group fits more than one layout in the PDF, so try the recorded selection order',
  REPORT_HANDLE_ALIAS: 'Two panel handles name the same number, so the PDF cannot be matched',
  REPORT_NO_MATCH: 'A panel group in this drawing has no matching layout left in the PDF',
  REPORT_BRIDGE_UNRESOLVED: 'Strings that cross panel groups in the PDF could not be placed in this drawing',
  REPORT_ROW_ANGLE_UNRESOLVED: 'The panel rows in this drawing have no single row angle to match against',
  REPORT_PDF_UNSUPPORTED: 'This PDF layout is not one the SolarEdge import can read',
  REPORT_LIMIT_EXCEEDED: 'This design is larger than a SolarEdge report can cover',
  REPORT_BUSY: 'Other PDFs are being read right now, so try again shortly',
  REPORT_WRITES_DRAINED: 'Drawing changes are paused right now, so try the report again shortly',
  REPORT_STORE_UNAVAILABLE: 'The drawing store is unavailable right now, so try again shortly',
  REPORT_ARTIFACT_CONFLICT: 'A stored report does not match this request, so the report stopped',
  REPORT_ARTIFACT_CORRUPT: 'The stored report failed its integrity check, so the report stopped',
  REPORT_ARTIFACT_INVALID: 'The report could not be stored, so it stopped',
  ARTIFACT_ID_INVALID: 'The file reference is not valid, so it cannot be downloaded',
  ARTIFACT_NOT_FOUND: 'The file was not found for this drawing',
  ARTIFACT_STALE: 'The drawing changed after this file was made, so make it again',
  ARTIFACT_CORRUPT: 'The stored file failed its integrity check',
  ARTIFACT_STORE_UNAVAILABLE: 'The file store is unavailable right now, so try again shortly',
  INVALID_SOLAREDGE_ACCEPT_REQUEST: 'The accept request is not valid, so make the report again',
  SOLAREDGE_REPORT_REQUIRED: 'Make the SolarEdge report before accepting its tracking',
  SOLAREDGE_REPORT_NOT_FOUND: 'The SolarEdge report was not found for this drawing, so make it again',
  SOLAREDGE_REPORT_UNAVAILABLE: 'The SolarEdge report could not be read right now, so try again shortly',
  SOLAREDGE_REPORT_CORRUPT: 'The stored SolarEdge report failed its integrity check, so make it again',
  SOLAREDGE_REPORT_KIND_MISMATCH: 'The chosen file is not a SolarEdge report, so make the report again',
  SOLAREDGE_REPORT_STALE: 'The drawing changed after the report was made, so make the report again',
  SOLAREDGE_REPORT_INVALID: 'The SolarEdge report no longer fits this drawing, so make it again',
  SOLAREDGE_REPORT_AMBIGUOUS: 'The report matched a panel group more than one way, so make it with the recorded order',
  STALE_GRAPH_REVISION: 'The solar design changed since this step opened, so try again',
  UNAUTHENTICATED: 'Sign in again to import a SolarEdge PDF',
  ENTITLEMENT_REQUIRED: 'Your plan does not include this SolarEdge import step',
  ENTITLEMENT_POLICY_UNAVAILABLE: 'The plan policy could not be read, so this step stays off',
  SOLAREDGE_CLIENT_REQUEST_INVALID: 'This SolarEdge step was given input it cannot send',
  SOLAREDGE_CLIENT_TIMEOUT: 'The server did not answer in time, so try again',
  SOLAREDGE_CLIENT_NETWORK: 'The server could not be reached, so try again',
  SOLAREDGE_CLIENT_ABORTED: 'This SolarEdge step was cancelled',
  SOLAREDGE_CLIENT_RESPONSE_INVALID: 'The server answer could not be read, so the step stopped',
  SOLAREDGE_CLIENT_ARTIFACT_MISMATCH: 'The downloaded file did not match its record, so it was not kept',
  SOLAREDGE_CLIENT_DIGEST_UNAVAILABLE: 'This browser cannot check the downloaded file, so it was not kept',
})

const CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/
const DRAWING_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/
const HEX64_PATTERN = /^[0-9a-f]{64}$/
const FILENAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.(pdf|json)$/
const EXTENSION_OF_MEDIA_TYPE = Object.freeze({ 'application/pdf': 'pdf', 'application/json': 'json' })
const MAX_SOURCE_VERSION = 2147483647
const MAX_PAGES = 50
const MAX_COUNT = 10_000_000
const MAX_API_BASE_LENGTH = 2048
const MAX_TIMEOUT_MS = 600_000
const ARTIFACT_REF_KEYS = Object.freeze([
  'schema', 'artifact_id', 'media_type', 'filename', 'byte_length', 'content_sha256', 'source_version', 'download',
])
const SOURCE_KEYS = Object.freeze([
  'schema', 'kind', 'drawing_id', 'project_id', 'source_version', 'graph_sha256', 'page_count', 'source',
])
const REPORT_KEYS = Object.freeze([
  'schema', 'kind', 'drawing_id', 'project_id', 'source_version', 'graph_sha256', 'source_artifact_id', 'counts',
  'report',
])
const COUNT_KEYS = Object.freeze([
  'pdf_matrices', 'pdf_panels', 'matchable_grids', 'bridge_grids', 'frames', 'matched_frames', 'group_strings',
  'bridge_strings', 'strings', 'assigned_panels', 'unassigned_panels', 'partial_strings',
])
const ENVELOPE_KEYS = Object.freeze(['error', 'degraded_mode'])

// Resolves to the sentence for a code; never echoes a malformed or oversized value.
export function solarEdgeReason(code) {
  if (typeof code === 'string' && Object.hasOwn(SOLAREDGE_IMPORT_REASONS, code)) return SOLAREDGE_IMPORT_REASONS[code]
  if (typeof code === 'string' && CODE_PATTERN.test(code)) return `${SOLAREDGE_IMPORT_FALLBACK} (${code})`
  return SOLAREDGE_IMPORT_FALLBACK
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

// True when the own keys are exactly `required` plus any subset of `optional`.
function hasExactKeys(value, required, optional = []) {
  const keys = Reflect.ownKeys(value)
  for (const key of keys) {
    if (typeof key !== 'string') return false
    if (!required.includes(key) && !optional.includes(key)) return false
  }
  return required.every((key) => Object.hasOwn(value, key))
}

function isIntegerIn(value, low, high) {
  return Number.isInteger(value) && value >= low && value <= high
}

function isProjectId(value) {
  return typeof value === 'string' && value.length >= 1 && value.length <= 100
}

function isDrawingId(value) {
  return typeof value === 'string' && DRAWING_ID_PATTERN.test(value)
}

function envelopeFieldsValid(body) {
  if (Object.hasOwn(body, 'error') && body.error !== null) return false
  if (Object.hasOwn(body, 'degraded_mode') && typeof body.degraded_mode !== 'boolean') return false
  return true
}

function copyRef(ref) {
  const copy = {}
  for (const key of ARTIFACT_REF_KEYS) copy[key] = ref[key]
  return copy
}

export function validateSolarArtifactRef(ref, drawingId, { mediaType, filename, maxBytes = SOLAR_ARTIFACT_MAX_BYTES } = {}) {
  if (!isPlainObject(ref) || !hasExactKeys(ref, ARTIFACT_REF_KEYS)) return false
  if (!isDrawingId(drawingId)) return false
  if (ref.schema !== 'leaf.solar-artifact-ref.v1') return false
  if (typeof ref.artifact_id !== 'string' || !HEX64_PATTERN.test(ref.artifact_id)) return false
  if (typeof ref.content_sha256 !== 'string' || !HEX64_PATTERN.test(ref.content_sha256)) return false
  if (typeof ref.media_type !== 'string' || !Object.hasOwn(EXTENSION_OF_MEDIA_TYPE, ref.media_type)) return false
  if (mediaType !== undefined && ref.media_type !== mediaType) return false
  if (typeof ref.filename !== 'string' || !FILENAME_PATTERN.test(ref.filename) || ref.filename.includes('..')) return false
  if (!ref.filename.endsWith(`.${EXTENSION_OF_MEDIA_TYPE[ref.media_type]}`)) return false
  if (filename !== undefined && ref.filename !== filename) return false
  if (!isIntegerIn(maxBytes, 1, Number.MAX_SAFE_INTEGER)) return false
  if (!isIntegerIn(ref.byte_length, 1, maxBytes)) return false
  if (!isIntegerIn(ref.source_version, 1, MAX_SOURCE_VERSION)) return false
  return ref.download === `/api/drawings/${drawingId}/artifacts/${ref.artifact_id}`
}

// The normalized upload value (8 keys, envelope fields dropped) or null.
export function validateSolarEdgeSource(body, drawingId) {
  if (!isPlainObject(body) || !hasExactKeys(body, SOURCE_KEYS, ENVELOPE_KEYS)) return null
  if (!envelopeFieldsValid(body)) return null
  if (body.schema !== 'leaf.solar-import-source.v1' || body.kind !== 'solaredge-pdf') return null
  if (!isDrawingId(drawingId) || body.drawing_id !== drawingId) return null
  if (body.project_id !== null && !isProjectId(body.project_id)) return null
  if (!isIntegerIn(body.source_version, 1, MAX_SOURCE_VERSION)) return null
  if (typeof body.graph_sha256 !== 'string' || !HEX64_PATTERN.test(body.graph_sha256)) return null
  if (!isIntegerIn(body.page_count, 1, MAX_PAGES)) return null
  if (!validateSolarArtifactRef(body.source, drawingId, {
    mediaType: 'application/pdf', filename: 'solaredge-source.pdf', maxBytes: SOLAREDGE_PDF_MAX_BYTES,
  })) return null
  if (body.source.source_version !== body.source_version) return null
  return {
    schema: body.schema,
    kind: body.kind,
    drawing_id: body.drawing_id,
    project_id: body.project_id,
    source_version: body.source_version,
    graph_sha256: body.graph_sha256,
    page_count: body.page_count,
    source: copyRef(body.source),
  }
}

// The normalized report value (9 keys, envelope fields dropped) or null.
export function validateSolarEdgeReport(body, drawingId, sourceArtifactId) {
  if (!isPlainObject(body) || !hasExactKeys(body, REPORT_KEYS, ENVELOPE_KEYS)) return null
  if (!envelopeFieldsValid(body)) return null
  if (body.schema !== 'leaf.solar-solaredge-report-result.v1' || body.kind !== 'solaredge-report') return null
  if (!isDrawingId(drawingId) || body.drawing_id !== drawingId) return null
  if (typeof sourceArtifactId !== 'string' || !HEX64_PATTERN.test(sourceArtifactId)) return null
  if (body.source_artifact_id !== sourceArtifactId) return null
  if (body.project_id !== null && !isProjectId(body.project_id)) return null
  if (!isIntegerIn(body.source_version, 1, MAX_SOURCE_VERSION)) return null
  if (typeof body.graph_sha256 !== 'string' || !HEX64_PATTERN.test(body.graph_sha256)) return null
  if (!isPlainObject(body.counts) || !hasExactKeys(body.counts, COUNT_KEYS)) return null
  const counts = {}
  for (const key of COUNT_KEYS) {
    if (!isIntegerIn(body.counts[key], 0, MAX_COUNT)) return null
    counts[key] = body.counts[key]
  }
  if (!validateSolarArtifactRef(body.report, drawingId, {
    mediaType: 'application/json', filename: 'solaredge-report.json',
  })) return null
  if (body.report.source_version !== body.source_version) return null
  return {
    schema: body.schema,
    kind: body.kind,
    drawing_id: body.drawing_id,
    project_id: body.project_id,
    source_version: body.source_version,
    graph_sha256: body.graph_sha256,
    source_artifact_id: body.source_artifact_id,
    counts,
    report: copyRef(body.report),
  }
}

function refuse(status, code, retryable) {
  return { ok: false, status, code, retryable }
}

function succeed(value) {
  return { ok: true, status: 200, value }
}

// UTF-8 byte length without TextEncoder; lone surrogates count as the 3-byte replacement.
function utf8Length(text) {
  let bytes = 0
  for (let index = 0; index < text.length; index += 1) {
    const unit = text.charCodeAt(index)
    if (unit < 0x80) bytes += 1
    else if (unit < 0x800) bytes += 2
    else if (unit >= 0xd800 && unit <= 0xdbff && index + 1 < text.length
      && text.charCodeAt(index + 1) >= 0xdc00 && text.charCodeAt(index + 1) <= 0xdfff) {
      bytes += 4
      index += 1
    } else bytes += 3
  }
  return bytes
}

function headerOf(response, name) {
  try {
    const value = response?.headers?.get?.(name)
    return typeof value === 'string' ? value : null
  } catch {
    return null
  }
}

// True when a present all-digit content-length is over the cap. More than 16 digits is over every
// cap; a value that is not all digits leaves the decision to the bytes actually read.
function declaredOver(response, cap) {
  const value = headerOf(response, 'content-length')
  if (value === null) return false
  const digits = value.trim()
  if (!/^\d+$/.test(digits)) return false
  return digits.length > 16 || Number(digits) > cap
}

const UNREADABLE = Object.freeze({ ok: false })
const OVERSIZE = Object.freeze({ ok: false, oversize: true })
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

// A monotonic clock where one exists, so a wall-clock step never moves a deadline.
function monotonicNow() {
  return typeof globalThis.performance?.now === 'function' ? globalThis.performance.now() : Date.now()
}

// One call's deadline and cancellation. The deadline runs from the call's start and covers the
// request, the headers and every body read; the caller's signal is forwarded while the call lives.
// Two things end a call at its deadline: the timer ends a stalled read, and expired() ends a flood
// of reads that are always ready (they settle on the microtask queue, so the timer never runs).
function openCall(timeoutMs, callerSignal) {
  const controller = new AbortController()
  const startedAt = monotonicNow()
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
  const timer = setTimeout(() => halt('timeout'), timeoutMs)
  if (callerSignal) {
    if (callerSignal.aborted) onCallerAbort()
    else callerSignal.addEventListener('abort', onCallerAbort, { once: true })
  }
  return {
    signal: controller.signal,
    timeoutMs,
    halt,
    reason: () => reason,
    // True when the call is stopped, or its budget has elapsed (then it halts with 'timeout').
    expired() {
      if (reason !== null) return true
      if (monotonicNow() - startedAt < timeoutMs) return false
      halt('timeout')
      return true
    },
    // Settles to { value }, { error } or STOPPED, whichever comes first; never rejects.
    race(work) {
      return Promise.race([
        Promise.resolve().then(work).then((value) => ({ value }), (error) => ({ error })),
        stopped,
      ])
    },
    // The failure for a stopped call: the deadline or the caller's abort, whichever came first.
    failure(status) {
      return reason === 'aborted'
        ? refuse(status, 'SOLAREDGE_CLIENT_ABORTED', false)
        : refuse(status, 'SOLAREDGE_CLIENT_TIMEOUT', true)
    },
    close() {
      clearTimeout(timer)
      callerSignal?.removeEventListener('abort', onCallerAbort)
    },
  }
}

function isByteChunk(chunk) {
  return Object.prototype.toString.call(chunk) === '[object Uint8Array]'
}

// Reads a stream chunk by chunk, refusing the moment the running byte total passes the cap.
async function readStream(call, body, cap) {
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
    if (step.done === true) break
    if (!isByteChunk(step.value)) {
      releaseBody(null, reader)
      return UNREADABLE
    }
    total += step.value.byteLength
    if (total > cap) {
      releaseBody(null, reader)
      return OVERSIZE
    }
    chunks.push(step.value)
  }
  const bytes = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  return { ok: true, bytes }
}

// Reads at most `cap` bytes of the body before the call stops. Resolves to STOPPED, UNREADABLE,
// OVERSIZE, { ok: true, bytes } or, from a bodiless text() fallback, { ok: true, text }.
async function readBody(call, response, cap, fallback) {
  if (declaredOver(response, cap)) {
    releaseBody(response)
    return OVERSIZE
  }
  let body
  try {
    body = response.body
  } catch {
    return UNREADABLE
  }
  if (body !== null && body !== undefined) {
    if (typeof body.getReader !== 'function') {
      releaseBody(response)
      return UNREADABLE
    }
    return readStream(call, body, cap)
  }
  if (typeof response[fallback] !== 'function') return UNREADABLE
  const outcome = await call.race(() => response[fallback]())
  if (outcome === STOPPED) return STOPPED
  if ('error' in outcome) return UNREADABLE
  if (call.expired()) {
    releaseBody(response)
    return STOPPED
  }
  const value = outcome.value
  if (fallback === 'text') {
    if (typeof value !== 'string') return UNREADABLE
    // Every UTF-16 unit is at least one UTF-8 byte, so the length alone can refuse without encoding.
    if (value.length > cap || new TextEncoder().encode(value).byteLength > cap) return OVERSIZE
    return { ok: true, text: value }
  }
  if (!(value instanceof ArrayBuffer)) return UNREADABLE
  if (value.byteLength > cap) return OVERSIZE
  return { ok: true, bytes: new Uint8Array(value) }
}

// Bounded JSON read: refuses an oversized declared or actual body (in bytes) and malformed UTF-8.
async function readBoundedJson(call, response) {
  const read = await readBody(call, response, SOLAREDGE_RESPONSE_MAX_BYTES, 'text')
  if (read === STOPPED) return STOPPED
  if (!read.ok) return UNREADABLE
  let text = read.text
  if (text === undefined) {
    try {
      text = new TextDecoder('utf-8', { fatal: true }).decode(read.bytes)
    } catch {
      return UNREADABLE
    }
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
    return refuse(status, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false)
  }
  const read = await readBoundedJson(call, response)
  if (read === STOPPED) return call.failure(status)
  if (!read.ok) return refuse(status, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', retryableServer)
  const body = read.value
  if (isPlainObject(body) && body.entitlement_required === true) {
    if (status === 403) return refuse(status, 'ENTITLEMENT_REQUIRED', false)
    if (status === 503) return refuse(status, 'ENTITLEMENT_POLICY_UNAVAILABLE', true)
    return refuse(status, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', retryableServer)
  }
  if (isPlainObject(body) && isPlainObject(body.error)
    && typeof body.error.reason_code === 'string' && CODE_PATTERN.test(body.error.reason_code)) {
    return refuse(status, body.error.reason_code, body.error.retryable === true)
  }
  return refuse(status, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', retryableServer)
}

async function defaultSha256Hex(bytes) {
  const subtle = globalThis.crypto?.subtle
  if (!subtle || typeof subtle.digest !== 'function') throw new Error('crypto.subtle is unavailable')
  const digest = new Uint8Array(await subtle.digest('SHA-256', bytes))
  let hex = ''
  for (const byte of digest) hex += byte.toString(16).padStart(2, '0')
  return hex
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

function checkTimeouts(timeouts) {
  if (timeouts === undefined) return { ...SOLAREDGE_TIMEOUTS_MS }
  if (!isPlainObject(timeouts)) throw new TypeError('timeouts must be a plain object')
  for (const key of Reflect.ownKeys(timeouts)) {
    if (typeof key !== 'string' || !Object.hasOwn(SOLAREDGE_TIMEOUTS_MS, key)) {
      throw new TypeError('timeouts may only name upload, report and artifact')
    }
    if (!isIntegerIn(timeouts[key], 1, MAX_TIMEOUT_MS)) {
      throw new TypeError('each timeout must be an integer from 1 to 600000 ms')
    }
  }
  return { ...SOLAREDGE_TIMEOUTS_MS, ...timeouts }
}

export function createSolarImportClient({ fetchImpl, apiBase = '', headers, onResponse, sha256Hex, timeouts } = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('fetchImpl must be a function')
  if (typeof headers !== 'function') throw new TypeError('headers must be a function')
  if (typeof apiBase !== 'string' || apiBase.length > MAX_API_BASE_LENGTH) {
    throw new TypeError('apiBase must be a string of at most 2048 characters')
  }
  if (onResponse !== undefined && typeof onResponse !== 'function') throw new TypeError('onResponse must be a function')
  if (sha256Hex !== undefined && typeof sha256Hex !== 'function') throw new TypeError('sha256Hex must be a function')
  const budgets = Object.freeze(checkTimeouts(timeouts))
  const digestOf = sha256Hex ?? defaultSha256Hex

  function requestHeaders(drawingId) {
    let value
    try {
      value = headers(drawingId)
    } catch {
      return null
    }
    if (!isPlainObject(value)) return null
    const copy = {}
    for (const key of Reflect.ownKeys(value)) {
      if (typeof key !== 'string' || typeof value[key] !== 'string') return null
      copy[key] = value[key]
    }
    return copy
  }

  // Opens one bounded call, runs it and always closes it. An invalid caller signal sends nothing.
  async function bounded(timeoutMs, signal, run) {
    if (!isSignal(signal)) return refuse(null, 'SOLAREDGE_CLIENT_REQUEST_INVALID', false)
    const call = openCall(timeoutMs, signal)
    try {
      return await run(call)
    } finally {
      call.close()
    }
  }

  // One exchange inside a call. Resolves to { failure } or, on a 200, { response, status }.
  async function exchange(call, { drawingId, path, query = '', method, extraHeaders = {}, body, signal }) {
    const base = requestHeaders(drawingId)
    if (base === null) return { failure: refuse(null, 'SOLAREDGE_CLIENT_REQUEST_INVALID', false) }
    const sent = { ...base, ...extraHeaders }
    const init = { method, headers: sent, signal: call.signal }
    if (body !== undefined) init.body = body
    // fetchImpl receives the call's own signal, so an abort after the headers still reaches the
    // transport; the budget's own timer is forwarded into the call as its deadline.
    const callFetch = (input, budgetInit) => {
      budgetInit?.signal?.addEventListener?.('abort', () => call.halt('timeout'), { once: true })
      return fetchImpl(input, { ...budgetInit, signal: call.signal })
    }
    const pending = Promise.resolve()
      .then(() => fetchWithBudget(callFetch, `${apiBase}${path}${query}`, init, call.timeoutMs))
    const outcome = await call.race(() => pending)
    if (outcome === STOPPED) {
      pending.then((late) => releaseBody(late), () => {})
      return { failure: call.failure(null) }
    }
    if ('error' in outcome) {
      if (outcome.error instanceof FetchTimeoutError || call.reason() === 'timeout') {
        return { failure: refuse(null, 'SOLAREDGE_CLIENT_TIMEOUT', true) }
      }
      if (call.reason() === 'aborted' || signal?.aborted === true) {
        return { failure: refuse(null, 'SOLAREDGE_CLIENT_ABORTED', false) }
      }
      return { failure: refuse(null, 'SOLAREDGE_CLIENT_NETWORK', true) }
    }
    const response = outcome.value
    if (onResponse) {
      const authorizationKey = Object.keys(sent).find((key) => key.toLowerCase() === 'authorization')
      try {
        onResponse(response, path, authorizationKey === undefined ? undefined : sent[authorizationKey])
      } catch {
        // The observer's failure never changes the result.
      }
    }
    const status = response?.status
    if (!Number.isInteger(status)) {
      releaseBody(response)
      return { failure: refuse(null, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false) }
    }
    if (status === 401) {
      releaseBody(response)
      return { failure: refuse(401, 'UNAUTHENTICATED', false) }
    }
    if (status !== 200) return { failure: await refusalOf(call, response, status) }
    return { response, status }
  }

  async function readJsonSuccess(call, response, validate) {
    const read = await readBoundedJson(call, response)
    if (read === STOPPED) return call.failure(200)
    const value = read.ok ? validate(read.value) : null
    return value === null ? refuse(200, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false) : succeed(value)
  }

  async function uploadPdf({ drawingId, file, projectId = null, signal } = {}) {
    try {
      if (!isDrawingId(drawingId)) return refuse(null, 'IMPORT_DRAWING_ID_INVALID', false)
      if (projectId !== null && !isProjectId(projectId)) return refuse(null, 'IMPORT_PROJECT_ID_INVALID', false)
      const size = sizeOf(file)
      if (size === null) return refuse(null, 'SOLAREDGE_CLIENT_REQUEST_INVALID', false)
      if (size === 0) return refuse(null, 'IMPORT_PDF_EMPTY', false)
      if (size > SOLAREDGE_PDF_MAX_BYTES) return refuse(null, 'IMPORT_PDF_TOO_LARGE', false)
      return await bounded(budgets.upload, signal, async (call) => {
        const exchanged = await exchange(call, {
          drawingId,
          path: `/api/drawings/${encodeURIComponent(drawingId)}/imports/solaredge-pdf`,
          query: projectId === null ? '' : `?project_id=${encodeURIComponent(projectId)}`,
          method: 'POST',
          extraHeaders: { 'Content-Type': 'application/pdf' },
          body: file,
          signal,
        })
        if (exchanged.failure) return exchanged.failure
        return readJsonSuccess(call, exchanged.response, (body) => validateSolarEdgeSource(body, drawingId))
      })
    } catch {
      return refuse(null, 'SOLAREDGE_CLIENT_REQUEST_INVALID', false)
    }
  }

  async function requestReport({
    drawingId, sourceArtifactId, alignmentTolerance, selectionOrder = 'unknown', projectId = null, signal,
  } = {}) {
    try {
      if (!isDrawingId(drawingId)) return refuse(null, 'REPORT_DRAWING_ID_INVALID', false)
      if (typeof sourceArtifactId !== 'string' || !HEX64_PATTERN.test(sourceArtifactId)
        || typeof alignmentTolerance !== 'number' || !Number.isFinite(alignmentTolerance)
        || alignmentTolerance < SOLAREDGE_MIN_ALIGNMENT_TOLERANCE
        || alignmentTolerance > SOLAREDGE_MAX_ALIGNMENT_TOLERANCE
        || !SOLAREDGE_SELECTION_ORDERS.includes(selectionOrder)
        || (projectId !== null && !isProjectId(projectId))) {
        return refuse(null, 'REPORT_REQUEST_INVALID', false)
      }
      const body = JSON.stringify({
        source_artifact_id: sourceArtifactId,
        alignment_tolerance: alignmentTolerance,
        selection_order: selectionOrder,
        ...(projectId === null ? {} : { project_id: projectId }),
      })
      if (utf8Length(body) > SOLAREDGE_REPORT_REQUEST_MAX_BYTES) return refuse(null, 'REPORT_REQUEST_TOO_LARGE', false)
      return await bounded(budgets.report, signal, async (call) => {
        const exchanged = await exchange(call, {
          drawingId,
          path: `/api/drawings/${encodeURIComponent(drawingId)}/imports/solaredge-pdf/report`,
          method: 'POST',
          extraHeaders: { 'Content-Type': 'application/json' },
          body,
          signal,
        })
        if (exchanged.failure) return exchanged.failure
        return readJsonSuccess(
          call, exchanged.response, (value) => validateSolarEdgeReport(value, drawingId, sourceArtifactId),
        )
      })
    } catch {
      return refuse(null, 'SOLAREDGE_CLIENT_REQUEST_INVALID', false)
    }
  }

  async function downloadArtifact({ drawingId, ref, current = false, signal, maxBytes = SOLAR_ARTIFACT_MAX_BYTES } = {}) {
    try {
      if (!isDrawingId(drawingId) || typeof current !== 'boolean'
        || !isIntegerIn(maxBytes, 1, SOLAR_ARTIFACT_MAX_BYTES)
        || !validateSolarArtifactRef(ref, drawingId, { maxBytes })) {
        return refuse(null, 'ARTIFACT_ID_INVALID', false)
      }
      const record = copyRef(ref)
      return await bounded(budgets.artifact, signal, async (call) => {
        const exchanged = await exchange(call, {
          drawingId,
          path: record.download,
          query: current ? '?current=true' : '',
          method: 'GET',
          signal,
        })
        if (exchanged.failure) return exchanged.failure
        const { response } = exchanged
        if (declaredOver(response, maxBytes)) {
          releaseBody(response)
          return refuse(200, 'SOLAREDGE_CLIENT_ARTIFACT_MISMATCH', false)
        }
        // More bytes than the record names is a mismatch, so the record's length is the read cap.
        const read = await readBody(call, response, record.byte_length, 'arrayBuffer')
        if (read === STOPPED) return call.failure(200)
        if (read === OVERSIZE) return refuse(200, 'SOLAREDGE_CLIENT_ARTIFACT_MISMATCH', false)
        if (!read.ok) return refuse(200, 'SOLAREDGE_CLIENT_RESPONSE_INVALID', false)
        const { bytes } = read
        if (bytes.byteLength !== record.byte_length) return refuse(200, 'SOLAREDGE_CLIENT_ARTIFACT_MISMATCH', false)
        if (headerOf(response, 'x-leaf-artifact-id') !== record.artifact_id) {
          return refuse(200, 'SOLAREDGE_CLIENT_ARTIFACT_MISMATCH', false)
        }
        const digest = await call.race(() => digestOf(bytes))
        if (digest === STOPPED) return call.failure(200)
        if ('error' in digest || typeof digest.value !== 'string') {
          return refuse(200, 'SOLAREDGE_CLIENT_DIGEST_UNAVAILABLE', false)
        }
        if (digest.value !== record.content_sha256) return refuse(200, 'SOLAREDGE_CLIENT_ARTIFACT_MISMATCH', false)
        return succeed({
          artifactId: record.artifact_id,
          mediaType: record.media_type,
          filename: record.filename,
          byteLength: record.byte_length,
          bytes,
        })
      })
    } catch {
      return refuse(null, 'SOLAREDGE_CLIENT_REQUEST_INVALID', false)
    }
  }

  return Object.freeze({ uploadPdf, requestReport, downloadArtifact })
}
