// The Studio side of the two Ground Physical terrain routes. Pure transport seam: no DOM, no
// storage, no api.js import; every dependency is injected. Each call is bounded (request size,
// response size, wall time) and never rejects: it resolves to { ok: true, status: 200, value } or
// { ok: false, status, code, retryable }. Fails closed: a success body that is not exactly the
// documented shape, or that does not describe the request it answers, is
// TERRAIN_CLIENT_RESPONSE_INVALID. The shapes live in solarTerrainModel.js.
//
// Routes (server/routers/solar_terrain.py): GET /api/drawings/{drawing_id}/terrain reads the
// stored terrain's view, and POST /api/drawings/{drawing_id}/terrain/operations runs mesh, slope
// or slope-clear on the head the request names (expected_head), as application/json. Both take
// an optional project_id in the query and no checkout header. Every refusal body carries its
// code, never prose, so this module owns the sentence for each key of the route's
// TERRAIN_ROUTE_REFUSALS. Everything the routes answer is a preview: no sentence here claims
// more, and none says what the server did or did not write after a call that lost its answer.
//
// Wall time: a call has one deadline, TERRAIN_TIMEOUT_MS unless the client was created with
// another, measured from the call's start. It covers the request checks, the headers, every body
// read and the work after the last read, so a server that sends headers and then stalls its body
// still settles the call, and a success is never returned once the budget has elapsed or the
// caller has aborted. A refusal already decided is returned as it is. The response body is
// bounded in bytes while reading, never after.
import { FetchTimeoutError, fetchWithBudget } from '../fetchBudget.js'
import {
  TERRAIN_OPERATIONS, resolveTerrainLimits, validateTerrainOperation, validateTerrainView,
} from './solarTerrainModel.js'

export const TERRAIN_REQUEST_MAX_BYTES = 8_192
export const TERRAIN_RESPONSE_MAX_BYTES = 65_536
export const TERRAIN_TIMEOUT_MS = 120_000
export const TERRAIN_MAX_TIMEOUT_MS = 600_000
export const TERRAIN_MAX_API_BASE_LENGTH = 2_048
export const TERRAIN_FALLBACK_SENTENCE = 'The terrain request stopped'

// One sentence per key of the route's TERRAIN_ROUTE_REFUSALS, and nothing else.
export const TERRAIN_ROUTE_REASONS = Object.freeze({
  TERRAIN_PROJECT_ID_INVALID: 'The project named for this terrain preview is not valid',
  TERRAIN_EXPECTED_HEAD_INVALID: 'Refresh the terrain preview before running this operation',
  TERRAIN_LIMITS_INVALID: 'Enter slope limits within the ranges shown',
  TERRAIN_DRAWING_NOT_FOUND: 'This drawing could not be found, so reopen it and try again',
  TERRAIN_STATE_NOT_FOUND: 'Import terrain for this drawing before running a preview',
  TERRAIN_GRAPH_REQUIRED: 'Start the solar design with Solar settings before previewing terrain',
  TERRAIN_PROJECT_MISMATCH: 'This drawing belongs to another project than the one named',
  TERRAIN_FRAME_UNSUPPORTED: 'This terrain preview needs the world frame without a grid transform',
  TERRAIN_GRID_MISSING: 'Import a terrain grid before running this preview',
  TERRAIN_NO_TRACKER_ROWS: 'Add tracker rows that can be checked against this terrain',
  TERRAIN_HEAD_MOVED: 'The terrain changed, so review the refreshed preview before running again',
  TERRAIN_GRID_INVALID: 'This terrain grid cannot be used for a preview',
  TERRAIN_GRID_TOO_LARGE: 'This terrain grid exceeds the preview limit of 300 nodes per side',
  TERRAIN_FRAMES_INVALID: 'The tracker frames cannot be used for this slope preview',
  TERRAIN_TOO_MANY_ROWS: 'This slope preview exceeds the supported tracker or sampling limits',
  TERRAIN_WRITES_DRAINED: 'Drawing changes are paused, so try the preview again shortly',
  TERRAIN_STORE_UNAVAILABLE: 'The drawing store is unavailable, so try again shortly',
  PHYSICAL_HEAD_CONFLICT: 'Another terrain change arrived first, so refresh before running again',
  PHYSICAL_HEAD_LOG_FULL: 'This drawing holds the most terrain changes it can keep',
  PHYSICAL_HEAD_PROJECT_MISMATCH: 'The terrain history belongs to another project',
  PHYSICAL_STATE_PROJECT_MISMATCH: 'The terrain record belongs to another project',
  PHYSICAL_HEAD_WRITES_DRAINED: 'Drawing changes are paused, so try the preview again shortly',
  PHYSICAL_STATE_WRITES_DRAINED: 'Drawing changes are paused, so try the preview again shortly',
  PHYSICAL_HEAD_STORE_UNAVAILABLE: 'The terrain history is unavailable, so try again shortly',
  PHYSICAL_STATE_STORE_UNAVAILABLE: 'The stored terrain is unavailable, so try again shortly',
  PHYSICAL_HEAD_CORRUPT: 'The terrain history failed its integrity check',
  PHYSICAL_HEAD_STORE_UNSAFE: 'This drawing store cannot record terrain changes safely',
  PHYSICAL_STATE_CORRUPT: 'The stored terrain failed its integrity check',
  TERRAIN_DRAWING_ID_INVALID: 'This drawing cannot show terrain because its id is not valid',
  TERRAIN_OPERATION_INVALID: 'Choose a mesh preview, slope preview or slope preview clear',
  TERRAIN_BODY_INVALID: 'This terrain operation was given input it cannot read',
  TERRAIN_BODY_TOO_LARGE: 'This terrain request is larger than the server accepts',
  TERRAIN_MEDIA_TYPE_REFUSED: 'This terrain operation was not sent in the required format',
  TERRAIN_OPERATION_FAILED: 'The terrain operation could not be completed, so refresh its state',
})

// The session answers every route can give and the codes this client decides itself.
// ENTITLEMENT_POLICY_UNAVAILABLE is this client's name for the 503 entitlement answer, which
// carries no reason code of its own.
export const TERRAIN_CLIENT_REASONS = Object.freeze({
  UNAUTHENTICATED: 'Sign in again to use terrain previews',
  FORBIDDEN: 'This session cannot access terrain previews',
  ENTITLEMENT_REQUIRED: 'Your workspace plan does not include this terrain action',
  ENTITLEMENT_POLICY_UNAVAILABLE: 'The workspace policy could not be read, so this action stays off',
  INTERNAL: 'The server could not complete this terrain request',
  BAD_PARAMS: 'The server could not read this terrain request',
  TERRAIN_CLIENT_REQUEST_INVALID: 'This terrain action was given input it cannot send',
  TERRAIN_CLIENT_TIMEOUT: 'The server did not answer in time, so refresh the terrain state',
  TERRAIN_CLIENT_NETWORK: 'The server could not be reached, so refresh before trying again',
  TERRAIN_CLIENT_ABORTED: 'This terrain request was cancelled here',
  TERRAIN_CLIENT_RESPONSE_INVALID: 'The server answer could not be read, so refresh the terrain state',
})

const CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/
const DRAWING_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/
const HEX64_PATTERN = /^[0-9a-f]{64}$/
const MAX_PROJECT_ID_CHARS = 100
const VIEW_OPTION_KEYS = Object.freeze(['drawingId', 'projectId', 'signal'])
const OPERATION_OPTION_KEYS = Object.freeze(['drawingId', 'projectId', 'operation', 'expectedHead', 'limits', 'signal'])

// Resolves to the sentence for a code; never echoes a malformed or oversized value.
export function terrainReason(code) {
  if (typeof code === 'string') {
    if (Object.hasOwn(TERRAIN_ROUTE_REASONS, code)) return TERRAIN_ROUTE_REASONS[code]
    if (Object.hasOwn(TERRAIN_CLIENT_REASONS, code)) return TERRAIN_CLIENT_REASONS[code]
    if (CODE_PATTERN.test(code)) return `${TERRAIN_FALLBACK_SENTENCE} (${code})`
  }
  return TERRAIN_FALLBACK_SENTENCE
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
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
// U+FFFD, built from its code so this source stays ASCII.
const REPLACEMENT_CHARACTER = String.fromCharCode(0xfffd)

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
        ? refuse(status, 'TERRAIN_CLIENT_ABORTED', false)
        : refuse(status, 'TERRAIN_CLIENT_TIMEOUT', true)
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
  if (declaredOver(response, TERRAIN_RESPONSE_MAX_BYTES)) {
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
      if (total > TERRAIN_RESPONSE_MAX_BYTES) {
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
    if (text.length > TERRAIN_RESPONSE_MAX_BYTES
      || new TextEncoder().encode(text).byteLength > TERRAIN_RESPONSE_MAX_BYTES) return UNREADABLE
    // A replaced invalid byte and literal U+FFFD are indistinguishable once decoded, so fail closed here as route envelopes never carry it.
    if (text.includes(REPLACEMENT_CHARACTER)) return UNREADABLE
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
    return refuse(status, 'TERRAIN_CLIENT_RESPONSE_INVALID', false)
  }
  const read = await readBoundedJson(call, response)
  if (read === STOPPED) return call.failure(status)
  if (!read.ok) return refuse(status, 'TERRAIN_CLIENT_RESPONSE_INVALID', retryableServer)
  const body = read.value
  if (isPlainObject(body) && body.entitlement_required === true) {
    if (status === 403) return refuse(status, 'ENTITLEMENT_REQUIRED', false)
    if (status === 503) return refuse(status, 'ENTITLEMENT_POLICY_UNAVAILABLE', true)
    return refuse(status, 'TERRAIN_CLIENT_RESPONSE_INVALID', retryableServer)
  }
  const error = isPlainObject(body) && isPlainObject(body.error) ? body.error : null
  if (error !== null && typeof error.reason_code === 'string' && CODE_PATTERN.test(error.reason_code)) {
    return refuse(status, error.reason_code, error.retryable === true)
  }
  // The gates in front of the route (server/deps.py, server/envelopes.py) answer with an error
  // code and prose but no reason code: a guest or unprovisioned session is 403 FORBIDDEN, the
  // identity authority and an unhandled fault are INTERNAL, and framework validation is 422
  // BAD_PARAMS. The prose is never shown.
  if (error !== null && status === 403 && error.error_code === 'FORBIDDEN') return refuse(status, 'FORBIDDEN', false)
  if (error !== null && status >= 400 && error.error_code === 'INTERNAL') {
    return refuse(status, 'INTERNAL', error.retryable === true)
  }
  if (error !== null && status >= 400 && status < 500 && error.error_code === 'BAD_PARAMS') {
    return refuse(status, 'BAD_PARAMS', false)
  }
  return refuse(status, 'TERRAIN_CLIENT_RESPONSE_INVALID', retryableServer)
}

function isSignal(signal) {
  return signal === undefined || signal === null
    || (typeof signal === 'object' && typeof signal.aborted === 'boolean'
      && typeof signal.addEventListener === 'function' && typeof signal.removeEventListener === 'function')
}

function hasOnlyKeys(options, allowed) {
  return Reflect.ownKeys(options).every((key) => typeof key === 'string' && allowed.includes(key))
}

export function createSolarTerrainClient({ fetchImpl, apiBase = '', headers, onResponse, timeoutMs } = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('fetchImpl must be a function')
  if (typeof headers !== 'function') throw new TypeError('headers must be a function')
  if (typeof apiBase !== 'string' || apiBase.length > TERRAIN_MAX_API_BASE_LENGTH) {
    throw new TypeError('apiBase must be a string of at most 2048 characters')
  }
  if (onResponse !== undefined && typeof onResponse !== 'function') throw new TypeError('onResponse must be a function')
  if (timeoutMs !== undefined && !isIntegerIn(timeoutMs, 1, TERRAIN_MAX_TIMEOUT_MS)) {
    throw new TypeError('timeoutMs must be an integer from 1 to 600000 ms')
  }
  const budget = timeoutMs ?? TERRAIN_TIMEOUT_MS

  // Read afresh for every request, so a session that changed its tenant or bearer is honored.
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

  // `body` is the JSON text of a POST, or undefined for a GET, which sends no body and no content type.
  async function exchange(call, { drawingId, url, body, signal }) {
    const base = requestHeaders(drawingId)
    if (base === null) return { failure: refuse(null, 'TERRAIN_CLIENT_REQUEST_INVALID', false) }
    const sent = Object.create(null)
    for (const key of Object.keys(base)) {
      if (key.toLowerCase() !== 'content-type') sent[key] = base[key]
    }
    const init = { method: 'GET', headers: sent, signal: call.signal }
    if (body !== undefined) {
      sent['Content-Type'] = 'application/json'
      init.method = 'POST'
      init.body = body
    }
    const callFetch = async (input, budgetInit) => {
      budgetInit?.signal?.addEventListener?.('abort', () => call.halt('timeout'), { once: true })
      const raw = Promise.resolve().then(() => fetchImpl(input, { ...budgetInit, signal: call.signal }))
      const settled = await call.race(() => raw)
      if (settled === STOPPED) {
        // The helper's finally must run now, so its timer and listener are released with the call.
        raw.then((late) => releaseBody(late), () => {})
        throw new Error('terrain call stopped')
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
        return { failure: refuse(null, 'TERRAIN_CLIENT_TIMEOUT', true) }
      }
      if (call.reason() === 'aborted' || signal?.aborted === true) {
        return { failure: refuse(null, 'TERRAIN_CLIENT_ABORTED', false) }
      }
      return { failure: refuse(null, 'TERRAIN_CLIENT_NETWORK', true) }
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
    // From here the answer is the response's: a response that throws while it is read is an
    // unreadable answer, never an unsendable request.
    let status
    try {
      status = response?.status
    } catch {
      status = undefined
    }
    if (!Number.isInteger(status)) {
      releaseBody(response)
      return { failure: refuse(null, 'TERRAIN_CLIENT_RESPONSE_INVALID', false) }
    }
    if (status === 401) {
      releaseBody(response)
      return { failure: refuse(401, 'UNAUTHENTICATED', false) }
    }
    if (status === 200) return { response }
    try {
      return { failure: await refusalOf(call, response, status) }
    } catch {
      return { failure: refuse(status, 'TERRAIN_CLIENT_RESPONSE_INVALID', status >= 500) }
    }
  }

  // One bounded exchange. `validate` turns the parsed success body into the normalized value or null.
  async function settle({ startedAt, drawingId, url, body, signal, validate }) {
    const call = openCall(budget, signal, startedAt)
    let success
    try {
      if (call.expired()) return call.failure(null)
      const exchanged = await exchange(call, { drawingId, url, body, signal })
      if (exchanged.failure) return exchanged.failure
      let read
      try {
        read = await readBoundedJson(call, exchanged.response)
      } catch {
        read = UNREADABLE
      }
      if (read === STOPPED) return call.failure(200)
      const value = read.ok ? validate(read.value) : null
      if (value === null) return refuse(200, 'TERRAIN_CLIENT_RESPONSE_INVALID', false)
      success = { ok: true, status: 200, value }
    } finally {
      call.close()
    }
    // The call is closed before its last check, so a success never leaves a call that has stopped: the
    // work after the last read (assembling the chunks, decoding, parsing, validating) and the cleanup of
    // the caller's signal are inside the deadline too, and an abort the caller made before we answer
    // counts. A refusal already decided above is returned as it is.
    try {
      if (signal?.aborted === true) call.halt('aborted')
    } catch {
      // A signal whose aborted getter throws leaves the answer as the call decided it.
    }
    return call.expired() ? call.failure(200) : success
  }

  // The route's path for a drawing, with the optional project in the query; null when the
  // project cannot be encoded.
  function routeUrl(drawingId, projectId, suffix) {
    let query = ''
    if (projectId !== null) {
      try {
        query = `?project_id=${encodeURIComponent(projectId)}`
      } catch {
        return null
      }
    }
    return `${apiBase}/api/drawings/${encodeURIComponent(drawingId)}/terrain${suffix}${query}`
  }

  async function getTerrain(options) {
    try {
      const startedAt = monotonicNow()
      if (options === undefined) options = {}
      if (options === null || typeof options !== 'object' || !hasOnlyKeys(options, VIEW_OPTION_KEYS)) {
        return refuse(null, 'TERRAIN_CLIENT_REQUEST_INVALID', false)
      }
      const { drawingId, projectId = null, signal } = options
      if (!isDrawingId(drawingId)) return refuse(null, 'TERRAIN_DRAWING_ID_INVALID', false)
      if (projectId !== null && !isProjectId(projectId)) return refuse(null, 'TERRAIN_PROJECT_ID_INVALID', false)
      if (!isSignal(signal)) return refuse(null, 'TERRAIN_CLIENT_REQUEST_INVALID', false)
      const url = routeUrl(drawingId, projectId, '')
      if (url === null) return refuse(null, 'TERRAIN_PROJECT_ID_INVALID', false)
      return await settle({
        startedAt, drawingId, url, body: undefined, signal,
        validate: (body) => validateTerrainView(body, { drawingId, projectId }),
      })
    } catch {
      return refuse(null, 'TERRAIN_CLIENT_REQUEST_INVALID', false)
    }
  }

  // The checks run in the route's own order. A slope request sends the complete resolved
  // limits; mesh and slope-clear send no limits key, and are refused here if given one.
  async function runTerrainOperation(options) {
    try {
      const startedAt = monotonicNow()
      if (options === undefined) options = {}
      if (options === null || typeof options !== 'object' || !hasOnlyKeys(options, OPERATION_OPTION_KEYS)) {
        return refuse(null, 'TERRAIN_CLIENT_REQUEST_INVALID', false)
      }
      const { drawingId, projectId = null, operation, expectedHead, limits, signal } = options
      if (!isDrawingId(drawingId)) return refuse(null, 'TERRAIN_DRAWING_ID_INVALID', false)
      if (projectId !== null && !isProjectId(projectId)) return refuse(null, 'TERRAIN_PROJECT_ID_INVALID', false)
      if (limits !== undefined && operation !== 'slope') return refuse(null, 'TERRAIN_BODY_INVALID', false)
      if (typeof operation !== 'string' || !TERRAIN_OPERATIONS.includes(operation)) {
        return refuse(null, 'TERRAIN_OPERATION_INVALID', false)
      }
      if (typeof expectedHead !== 'string' || !HEX64_PATTERN.test(expectedHead)) {
        return refuse(null, 'TERRAIN_EXPECTED_HEAD_INVALID', false)
      }
      const payload = { operation, expected_head: expectedHead }
      const request = { drawingId, projectId, operation, expectedHead }
      if (operation === 'slope') {
        const resolved = resolveTerrainLimits(limits)
        if (resolved === null) return refuse(null, 'TERRAIN_LIMITS_INVALID', false)
        payload.limits = resolved
        request.limits = resolved
      }
      if (!isSignal(signal)) return refuse(null, 'TERRAIN_CLIENT_REQUEST_INVALID', false)
      const url = routeUrl(drawingId, projectId, '/operations')
      if (url === null) return refuse(null, 'TERRAIN_PROJECT_ID_INVALID', false)
      const body = JSON.stringify(payload)
      if (new TextEncoder().encode(body).byteLength > TERRAIN_REQUEST_MAX_BYTES) {
        return refuse(null, 'TERRAIN_BODY_TOO_LARGE', false)
      }
      return await settle({
        startedAt, drawingId, url, body, signal,
        validate: (answer) => validateTerrainOperation(answer, request),
      })
    } catch {
      return refuse(null, 'TERRAIN_CLIENT_REQUEST_INVALID', false)
    }
  }

  return Object.freeze({ getTerrain, runTerrainOperation })
}
