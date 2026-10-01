// The Studio side of the combiner intake import route. Pure transport seam: no DOM, no storage,
// no api.js import; every dependency is injected. The one call is bounded (request size, response
// size, wall time) and never rejects: it resolves to { ok: true, status: 200, value } or
// { ok: false, status, code, retryable }. Fails closed: a success body that is not exactly the
// documented shape, or that does not describe the request it answers, is
// COMBINER_CLIENT_RESPONSE_INVALID.
//
// Route (server/routers/drawings.py): POST /api/drawings/{drawing_id}/imports/combiner-intake with
// the import's JSON bytes as the body (application/json) and an optional project_id in the query.
// The body is one JSON object with exactly "combiner_intake" and "panel_groups"; the server parses,
// bounds and binds it, so this client sends the caller's bytes unchanged and never parses them.
// Every refusal body carries its code, never prose, so this module owns the sentence for each key
// of the route's COMBINER_INTAKE_IMPORT_REFUSALS. The guest gate answers 403 with no reason code,
// which this client reads as FORBIDDEN.
//
// Wall time: the call has one deadline, COMBINER_INTAKE_TIMEOUT_MS unless the client was created
// with another, measured from the call's start, before the arguments are read. It covers the
// prechecks, the request, the headers, every body read and the work after the last read, so a
// server that sends headers and then stalls its body still settles the call, and a success is
// never returned once the budget has elapsed. The response body is bounded in bytes while
// reading, never after.
import { FetchTimeoutError, fetchWithBudget } from '../fetchBudget.js'

export const COMBINER_INTAKE_MAX_BYTES = 16_777_216
export const COMBINER_INTAKE_RESPONSE_MAX_BYTES = 65_536
export const COMBINER_INTAKE_TIMEOUT_MS = 120_000
export const COMBINER_INTAKE_FALLBACK = 'The combiner intake import stopped'

export const COMBINER_INTAKE_REASONS = Object.freeze({
  COMBINER_IMPORT_DRAWING_ID_INVALID: 'This drawing cannot take a combiner intake because its id is not valid',
  COMBINER_IMPORT_PROJECT_ID_INVALID: 'The project named for this import is not valid',
  COMBINER_IMPORT_MEDIA_TYPE_REFUSED: 'The intake was not sent as JSON, so choose the intake file again',
  COMBINER_IMPORT_EMPTY: 'The chosen intake file is empty, so choose the intake file again',
  COMBINER_IMPORT_TOO_LARGE: 'The intake file is larger than 16 MiB, more than an import accepts',
  COMBINER_IMPORT_ENCODING_INVALID: 'The intake file is not UTF-8 text without a byte order mark, so export it again',
  COMBINER_IMPORT_JSON_INVALID: 'The intake file is not valid JSON, so export it again',
  COMBINER_IMPORT_BODY_INVALID: 'The intake file must hold exactly the combiner intake and the panel group outlines',
  COMBINER_INTAKE_INVALID: 'The combiner intake is not a combiner placement dump this import can read',
  COMBINER_OUTLINES_INVALID: 'The panel group outlines are missing or not valid, so export them again',
  COMBINER_IMPORT_CHECKOUT_DENIED: 'Someone else has this drawing checked out, so the intake was not saved',
  COMBINER_IMPORT_DRAWING_NOT_FOUND: 'This drawing could not be found, so reopen it and try again',
  COMBINER_IMPORT_GRAPH_REQUIRED: 'Start the solar design with Solar settings before importing a combiner intake',
  COMBINER_IMPORT_GRAPH_NOT_LOCAL: 'This drawing keeps its solar design elsewhere, so it cannot take a combiner intake',
  COMBINER_IMPORT_PROJECT_MISMATCH: 'This drawing belongs to another project than the one named for the import',
  COMBINER_IMPORT_HEAD_MOVED: 'Another change reached this drawing first, so try the import again',
  COMBINER_L2_MODE_REQUIRED: 'Turn on L2 collectors in Solar settings before importing a combiner intake',
  COMBINER_EXISTING_L1: 'This drawing already has combiners, so the intake was not saved',
  COMBINER_INTAKE_UNITS_MISMATCH: 'The intake was recorded in other drawing units than this drawing',
  COMBINER_INTAKE_CONTEXT_MISMATCH: 'The intake was recorded with other collector settings than this drawing',
  COMBINER_INTAKE_L2_MISMATCH: 'The L2 collectors in the intake do not match the ones in this drawing',
  COMBINER_INTAKE_STRING_MISMATCH: 'The strings in the intake do not match the ones in this drawing',
  COMBINER_IMPORT_INTAKE_TOO_LARGE: 'This drawing cannot hold this intake beside its solar design, so nothing was saved',
  COMBINER_IMPORT_SOURCE_CORRUPT: 'The stored drawing failed its integrity check, so nothing was saved',
  COMBINER_IMPORT_WRITE_REFUSED: 'The drawing store refused the new version, so nothing was saved',
  COMBINER_IMPORT_FAILED: 'The combiner intake could not be imported, so nothing was saved',
  COMBINER_IMPORT_WRITES_DRAINED: 'Drawing changes are paused right now, so try the import again shortly',
  COMBINER_IMPORT_STORE_UNAVAILABLE: 'The drawing store is unavailable right now, so try again shortly',
  COMBINER_IMPORT_CHECKOUT_UNAVAILABLE: 'The drawing checkout could not be checked right now, so try again shortly',
  UNAUTHENTICATED: 'Sign in again to import a combiner intake',
  FORBIDDEN: 'A guest session cannot import a combiner intake, so sign in to an account',
  ENTITLEMENT_REQUIRED: 'Your plan does not include uploads, so this import stays off',
  ENTITLEMENT_POLICY_UNAVAILABLE: 'The plan policy could not be read, so this step stays off',
  COMBINER_CLIENT_REQUEST_INVALID: 'This combiner intake step was given input it cannot send',
  COMBINER_CLIENT_TIMEOUT: 'The server did not answer in time, so try again',
  COMBINER_CLIENT_NETWORK: 'The server could not be reached, so try again',
  COMBINER_CLIENT_ABORTED: 'This combiner intake step was cancelled',
  COMBINER_CLIENT_RESPONSE_INVALID: 'The server answer could not be read, so the step stopped',
})

const CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/
const DRAWING_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/
const HEX64_PATTERN = /^[0-9a-f]{64}$/
const RESULT_SCHEMA = 'leaf.solar-combiner-intake-import.v1'
// The server's own bounds: solar_combiner_intake_import.MAX_PROJECT_ID_CHARS and MAX_GROUPS, the
// design graph schema's rev maximum, solar_inverter_combiner.MAX_L2 and MAX_STRINGS (the binding
// counts) and solar_inverter_cabling.MAX_OUTLINE_VERTICES_TOTAL. The store names no version maximum,
// so a version is any safe integer from 1.
const MAX_PROJECT_ID_CHARS = 100
const MAX_GROUPS = 10_000
const MAX_GRAPH_REV = 1_000_000
const MAX_L2 = 1_000
const MAX_STRINGS = 20_000
const MAX_OUTLINE_VERTICES = 1_000_000
const MAX_API_BASE_LENGTH = 2048
const MAX_TIMEOUT_MS = 600_000
const RESULT_KEYS = Object.freeze([
  'schema_version', 'drawing_id', 'project_id', 'created', 'version', 'parent_version', 'graph_sha256',
  'graph_rev', 'intake_sha256', 'combiner_intake_sha256', 'panel_groups_sha256', 'bound', 'panel_groups',
  'outline_vertices',
])
const ENVELOPE_KEYS = Object.freeze(['error', 'degraded_mode'])
const BOUND_KEYS = Object.freeze(['l2_inverters', 'strings'])

// Resolves to the sentence for a code; never echoes a malformed or oversized value.
export function combinerIntakeReason(code) {
  if (typeof code === 'string' && Object.hasOwn(COMBINER_INTAKE_REASONS, code)) return COMBINER_INTAKE_REASONS[code]
  if (typeof code === 'string' && CODE_PATTERN.test(code)) return `${COMBINER_INTAKE_FALLBACK} (${code})`
  return COMBINER_INTAKE_FALLBACK
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

function isVersion(value) {
  return Number.isSafeInteger(value) && value >= 1
}

// 1 to 100 code points, the route's own bound (Python len counts code points).
function isProjectId(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 2 * MAX_PROJECT_ID_CHARS) return false
  return [...value].length <= MAX_PROJECT_ID_CHARS
}

function isDrawingId(value) {
  return typeof value === 'string' && DRAWING_ID_PATTERN.test(value)
}

function isHex64(value) {
  return typeof value === 'string' && HEX64_PATTERN.test(value)
}

// A new version's parent is the head it replaced; an unchanged import names the head's own parent,
// which is null for a drawing's first version. A parent is always older than its version.
function validParent(parent, version, created) {
  if (parent === null) return !created
  return isVersion(parent) && parent < version
}

// The normalized import result (14 keys, envelope fields dropped) or null. `request` is the
// request it answers: { drawingId, projectId }.
export function validateCombinerIntakeImport(body, request) {
  if (!isPlainObject(request)) return null
  const { drawingId, projectId = null } = request
  if (!isDrawingId(drawingId)) return null
  if (!isPlainObject(body) || !hasExactKeys(body, RESULT_KEYS, ENVELOPE_KEYS)) return null
  if (Object.hasOwn(body, 'error') && body.error !== null) return null
  if (Object.hasOwn(body, 'degraded_mode') && typeof body.degraded_mode !== 'boolean') return null
  if (body.schema_version !== RESULT_SCHEMA || typeof body.created !== 'boolean') return null
  if (body.drawing_id !== drawingId || !isProjectId(body.project_id)) return null
  if (projectId !== null && body.project_id !== projectId) return null
  if (!isVersion(body.version) || !validParent(body.parent_version, body.version, body.created)) return null
  if (!isIntegerIn(body.graph_rev, 0, MAX_GRAPH_REV)) return null
  for (const key of ['graph_sha256', 'intake_sha256', 'combiner_intake_sha256', 'panel_groups_sha256']) {
    if (!isHex64(body[key])) return null
  }
  const bound = body.bound
  if (!isPlainObject(bound) || !hasExactKeys(bound, BOUND_KEYS)) return null
  if (!isIntegerIn(bound.l2_inverters, 0, MAX_L2) || !isIntegerIn(bound.strings, 0, MAX_STRINGS)) return null
  if (!isIntegerIn(body.panel_groups, 1, MAX_GROUPS)) return null
  if (!isIntegerIn(body.outline_vertices, 0, MAX_OUTLINE_VERTICES)) return null
  const value = {}
  for (const key of RESULT_KEYS) value[key] = body[key]
  value.bound = { l2_inverters: bound.l2_inverters, strings: bound.strings }
  return value
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

// One call's deadline and cancellation (the solarLandxmlClient.js design): the timer ends a
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
        ? refuse(status, 'COMBINER_CLIENT_ABORTED', false)
        : refuse(status, 'COMBINER_CLIENT_TIMEOUT', true)
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
  if (declaredOver(response, COMBINER_INTAKE_RESPONSE_MAX_BYTES)) {
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
      if (total > COMBINER_INTAKE_RESPONSE_MAX_BYTES) {
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
    if (text.length > COMBINER_INTAKE_RESPONSE_MAX_BYTES
      || new TextEncoder().encode(text).byteLength > COMBINER_INTAKE_RESPONSE_MAX_BYTES) return UNREADABLE
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
    return refuse(status, 'COMBINER_CLIENT_RESPONSE_INVALID', false)
  }
  const read = await readBoundedJson(call, response)
  if (read === STOPPED) return call.failure(status)
  if (!read.ok) return refuse(status, 'COMBINER_CLIENT_RESPONSE_INVALID', retryableServer)
  const body = read.value
  if (isPlainObject(body) && body.entitlement_required === true) {
    if (status === 403) return refuse(status, 'ENTITLEMENT_REQUIRED', false)
    if (status === 503) return refuse(status, 'ENTITLEMENT_POLICY_UNAVAILABLE', true)
    return refuse(status, 'COMBINER_CLIENT_RESPONSE_INVALID', retryableServer)
  }
  if (isPlainObject(body) && isPlainObject(body.error)
    && typeof body.error.reason_code === 'string' && CODE_PATTERN.test(body.error.reason_code)) {
    return refuse(status, body.error.reason_code, body.error.retryable === true)
  }
  // The guest gate (server/deps.py) answers 403 FORBIDDEN with no reason code.
  if (status === 403 && isPlainObject(body) && isPlainObject(body.error) && body.error.error_code === 'FORBIDDEN') {
    return refuse(status, 'FORBIDDEN', false)
  }
  return refuse(status, 'COMBINER_CLIENT_RESPONSE_INVALID', retryableServer)
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

export function createSolarCombinerIntakeClient({ fetchImpl, apiBase = '', headers, onResponse, timeoutMs } = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('fetchImpl must be a function')
  if (typeof headers !== 'function') throw new TypeError('headers must be a function')
  if (typeof apiBase !== 'string' || apiBase.length > MAX_API_BASE_LENGTH) {
    throw new TypeError('apiBase must be a string of at most 2048 characters')
  }
  if (onResponse !== undefined && typeof onResponse !== 'function') throw new TypeError('onResponse must be a function')
  if (timeoutMs !== undefined && !isIntegerIn(timeoutMs, 1, MAX_TIMEOUT_MS)) {
    throw new TypeError('timeoutMs must be an integer from 1 to 600000 ms')
  }
  const budget = timeoutMs ?? COMBINER_INTAKE_TIMEOUT_MS

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
    if (base === null) return { failure: refuse(null, 'COMBINER_CLIENT_REQUEST_INVALID', false) }
    const sent = Object.create(null)
    for (const key of Object.keys(base)) {
      if (key.toLowerCase() !== 'content-type') sent[key] = base[key]
    }
    sent['Content-Type'] = 'application/json'
    const init = { method: 'POST', headers: sent, body: file, signal: call.signal }
    const callFetch = async (input, budgetInit) => {
      budgetInit?.signal?.addEventListener?.('abort', () => call.halt('timeout'), { once: true })
      const raw = Promise.resolve().then(() => fetchImpl(input, { ...budgetInit, signal: call.signal }))
      const settled = await call.race(() => raw)
      if (settled === STOPPED) {
        // The helper's finally must run now, so its timer and listener are released with the call.
        raw.then((late) => releaseBody(late), () => {})
        throw new Error('combiner intake call stopped')
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
        return { failure: refuse(null, 'COMBINER_CLIENT_TIMEOUT', true) }
      }
      if (call.reason() === 'aborted' || signal?.aborted === true) {
        return { failure: refuse(null, 'COMBINER_CLIENT_ABORTED', false) }
      }
      return { failure: refuse(null, 'COMBINER_CLIENT_NETWORK', true) }
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
      return { failure: refuse(null, 'COMBINER_CLIENT_RESPONSE_INVALID', false) }
    }
    if (status === 401) {
      releaseBody(response)
      return { failure: refuse(401, 'UNAUTHENTICATED', false) }
    }
    if (status !== 200) return { failure: await refusalOf(call, response, status) }
    return { response }
  }

  async function importCombinerIntake(options) {
    try {
      const startedAt = monotonicNow()
      if (options === undefined) options = {}
      if (options === null || typeof options !== 'object') {
        return refuse(null, 'COMBINER_CLIENT_REQUEST_INVALID', false)
      }
      const { drawingId, file, projectId = null, signal } = options
      if (!isDrawingId(drawingId)) return refuse(null, 'COMBINER_IMPORT_DRAWING_ID_INVALID', false)
      if (projectId !== null && !isProjectId(projectId)) {
        return refuse(null, 'COMBINER_IMPORT_PROJECT_ID_INVALID', false)
      }
      const size = sizeOf(file)
      if (size === null) return refuse(null, 'COMBINER_CLIENT_REQUEST_INVALID', false)
      if (size === 0) return refuse(null, 'COMBINER_IMPORT_EMPTY', false)
      if (size > COMBINER_INTAKE_MAX_BYTES) return refuse(null, 'COMBINER_IMPORT_TOO_LARGE', false)
      if (!isSignal(signal)) return refuse(null, 'COMBINER_CLIENT_REQUEST_INVALID', false)
      let query = ''
      if (projectId !== null) {
        try {
          query = `?project_id=${encodeURIComponent(projectId)}`
        } catch {
          return refuse(null, 'COMBINER_IMPORT_PROJECT_ID_INVALID', false)
        }
      }
      const url = `${apiBase}/api/drawings/${encodeURIComponent(drawingId)}/imports/combiner-intake${query}`
      const request = { drawingId, projectId }
      const call = openCall(budget, signal, startedAt)
      let result
      try {
        if (call.expired()) return call.failure(null)
        const exchanged = await exchange(call, { drawingId, url, file, signal })
        if (exchanged.failure) return exchanged.failure
        const read = await readBoundedJson(call, exchanged.response)
        if (read === STOPPED) return call.failure(200)
        const value = read.ok ? validateCombinerIntakeImport(read.value, request) : null
        if (value === null) return refuse(200, 'COMBINER_CLIENT_RESPONSE_INVALID', false)
        result = { ok: true, status: 200, value }
      } finally {
        call.close()
      }
      // The call is closed before its last check, so a success never leaves a call that has stopped:
      // the work after the last read (assembling the chunks, parsing, validating) and the cleanup of
      // the caller's signal are inside the deadline too, and an abort made before we answer counts.
      if (result.ok === true) {
        try {
          if (signal?.aborted === true) call.halt('aborted')
        } catch {
          // A signal whose aborted getter throws leaves the answer as the call decided it.
        }
        if (call.expired()) return call.failure(200)
      }
      return result
    } catch {
      return refuse(null, 'COMBINER_CLIENT_REQUEST_INVALID', false)
    }
  }

  return Object.freeze({ importCombinerIntake })
}
