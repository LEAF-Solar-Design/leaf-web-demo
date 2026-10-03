// Tracker row transport. The private lifecycle follows solarTerrainClient; no automatic retry.
import { FetchTimeoutError, fetchWithBudget } from '../fetchBudget.js'
import { validateTrackerRowsRequest, validateTrackerRowsResult } from './solarTrackerRowsModel.js'
import { TRACKER_ROWS_REASONS } from './solarTrackerRowsReasons.js'

export const TRACKER_ROWS_RESPONSE_MAX_BYTES = 65_536
export const TRACKER_ROWS_TIMEOUT_MS = 120_000
export const TRACKER_ROWS_MAX_TIMEOUT_MS = 600_000
export const TRACKER_ROWS_MAX_API_BASE_LENGTH = 2_048
export const TRACKER_ROWS_FALLBACK_SENTENCE = 'The tracker row request stopped'

export const TRACKER_ROWS_CLIENT_REASONS = Object.freeze({
  UNAUTHENTICATED: 'Sign in again to create tracker rows',
  FORBIDDEN: 'This session cannot create tracker rows',
  ENTITLEMENT_REQUIRED: 'Your workspace plan does not include creating tracker rows',
  ENTITLEMENT_POLICY_UNAVAILABLE: 'The workspace policy could not be read, so this action stays off',
  INTERNAL: 'The server could not complete this tracker row request',
  BAD_PARAMS: 'The server could not read this tracker row request',
  TRACKER_ROWS_CLIENT_REQUEST_INVALID: 'This tracker row action was given input it cannot send',
  TRACKER_ROWS_CLIENT_TIMEOUT: 'The server did not answer in time, so refresh the physical state',
  TRACKER_ROWS_CLIENT_NETWORK: 'The server could not be reached, so refresh before trying again',
  TRACKER_ROWS_CLIENT_ABORTED: 'This tracker row request was cancelled here',
  TRACKER_ROWS_CLIENT_RESPONSE_INVALID: 'The server answer could not be read, so refresh the physical state',
})

const CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/
const DRAWING_ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,62}$/
const MAX_PROJECT_ID_CHARS = 100
const OPTION_KEYS = Object.freeze(['drawingId', 'projectId', 'drawingUnits', 'request', 'signal'])

// Resolves to the sentence for a code; never echoes a malformed or oversized value.
export function trackerRowsReason(code) {
  if (typeof code === 'string') {
    if (Object.hasOwn(TRACKER_ROWS_REASONS, code)) return TRACKER_ROWS_REASONS[code]
    if (Object.hasOwn(TRACKER_ROWS_CLIENT_REASONS, code)) return TRACKER_ROWS_CLIENT_REASONS[code]
    if (CODE_PATTERN.test(code)) return `${TRACKER_ROWS_FALLBACK_SENTENCE} (${code})`
  }
  return TRACKER_ROWS_FALLBACK_SENTENCE
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
  if ([...value].length > MAX_PROJECT_ID_CHARS) return false
  try { encodeURIComponent(value) } catch { return false }
  return true
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
        ? refuse(status, 'TRACKER_ROWS_CLIENT_ABORTED', false)
        : refuse(status, 'TRACKER_ROWS_CLIENT_TIMEOUT', true)
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

const TYPED_ARRAY_PROTOTYPE = Object.getPrototypeOf(Uint8Array.prototype)
const typedArrayTag = Object.getOwnPropertyDescriptor(TYPED_ARRAY_PROTOTYPE, Symbol.toStringTag).get
const typedArrayByteLength = Object.getOwnPropertyDescriptor(TYPED_ARRAY_PROTOTYPE, 'byteLength').get

// A chunk's byte length read through the intrinsic getters, so an own or inherited byteLength
// or Symbol.toStringTag on the chunk cannot lie; -1 for anything that is not a Uint8Array.
function byteChunkLength(chunk) {
  try {
    return typedArrayTag.call(chunk) === 'Uint8Array' ? typedArrayByteLength.call(chunk) : -1
  } catch {
    return -1
  }
}

// Bounded JSON read: refuses an oversized declared or actual body (in bytes) and malformed
// UTF-8. A streamed chunk is copied into one bounded buffer as it is read and never kept.
// Resolves to STOPPED, UNREADABLE or { ok: true, value }.
async function readBoundedJson(call, response) {
  if (declaredOver(response, TRACKER_ROWS_RESPONSE_MAX_BYTES)) {
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
    let reader
    try {
      if (typeof body.getReader !== 'function') {
        releaseBody(response)
        return UNREADABLE
      }
      reader = body.getReader()
    } catch {
      releaseBody(response)
      return UNREADABLE
    }
    const bytes = new Uint8Array(TRACKER_ROWS_RESPONSE_MAX_BYTES)
    let total = 0
    let finished = false
    try {
      for (;;) {
        if (call.expired()) return STOPPED
        const outcome = await call.race(() => reader.read())
        if (outcome === STOPPED) return STOPPED
        const step = 'error' in outcome ? null : outcome.value
        if (step === null || typeof step !== 'object') return UNREADABLE
        if (step.done === true) {
          finished = true
          if (call.expired()) return STOPPED
          break
        }
        const chunk = step.value
        const length = byteChunkLength(chunk)
        if (length < 0 || length > TRACKER_ROWS_RESPONSE_MAX_BYTES - total) return UNREADABLE
        bytes.set(chunk, total)
        total += length
      }
    } catch {
      return UNREADABLE
    } finally {
      if (!finished) releaseBody(null, reader)
    }
    try {
      text = new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(0, total))
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
    if (text.length > TRACKER_ROWS_RESPONSE_MAX_BYTES
      || new TextEncoder().encode(text).byteLength > TRACKER_ROWS_RESPONSE_MAX_BYTES) return UNREADABLE
    // A replaced invalid byte and literal U+FFFD are indistinguishable once decoded, so fail closed here as route envelopes never carry it.
    if (text.includes(REPLACEMENT_CHARACTER)) return UNREADABLE
  }
  try {
    return { ok: true, value: JSON.parse(text) }
  } catch {
    return UNREADABLE
  }
}

// Maps an answer outside the two success statuses and 401 to a failure result.
async function refusalOf(call, response, status) {
  const retryableServer = status >= 500
  if (status >= 200 && status < 300) {
    releaseBody(response)
    return refuse(status, 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID', false)
  }
  const read = await readBoundedJson(call, response)
  if (read === STOPPED) return call.failure(status)
  if (!read.ok) return refuse(status, 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID', retryableServer)
  const body = read.value
  const error = isPlainObject(body) && isPlainObject(body.error) ? body.error : null
  if (error !== null && typeof error.reason_code === 'string' && CODE_PATTERN.test(error.reason_code)) {
    return refuse(status, error.reason_code, error.retryable === true)
  }
  if (isPlainObject(body) && body.entitlement_required === true) {
    if (status === 403) return refuse(status, 'ENTITLEMENT_REQUIRED', false)
    if (status === 503) return refuse(status, 'ENTITLEMENT_POLICY_UNAVAILABLE', true)
    return refuse(status, 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID', retryableServer)
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
  return refuse(status, 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID', retryableServer)
}

function isSignal(signal) {
  return signal === undefined || signal === null
    || (typeof signal === 'object' && typeof signal.aborted === 'boolean'
      && typeof signal.addEventListener === 'function' && typeof signal.removeEventListener === 'function')
}

function hasOnlyKeys(options, allowed) {
  return Reflect.ownKeys(options).every((key) => typeof key === 'string' && allowed.includes(key))
}

export function createSolarTrackerRowsClient({ fetchImpl, apiBase = '', headers, onResponse, timeoutMs } = {}) {
  if (typeof fetchImpl !== 'function') throw new TypeError('fetchImpl must be a function')
  if (typeof headers !== 'function') throw new TypeError('headers must be a function')
  if (typeof apiBase !== 'string' || apiBase.length > TRACKER_ROWS_MAX_API_BASE_LENGTH) {
    throw new TypeError('apiBase must be a string of at most 2048 characters')
  }
  if (onResponse !== undefined && typeof onResponse !== 'function') throw new TypeError('onResponse must be a function')
  if (timeoutMs !== undefined && !isIntegerIn(timeoutMs, 1, TRACKER_ROWS_MAX_TIMEOUT_MS)) {
    throw new TypeError('timeoutMs must be an integer from 1 to 600000 ms')
  }
  const budget = timeoutMs ?? TRACKER_ROWS_TIMEOUT_MS

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
      if (typeof key !== 'string') return null
      const entry = value[key]
      if (typeof entry !== 'string') return null
      copy[key] = entry
    }
    return copy
  }

  // Read the injected headers afresh and send one compact JSON POST.
  async function exchange(call, { drawingId, url, body, signal }) {
    const base = requestHeaders(drawingId)
    if (base === null) return { failure: refuse(null, 'TRACKER_ROWS_CLIENT_REQUEST_INVALID', false) }
    const sent = Object.create(null)
    for (const key of Object.keys(base)) {
      if (key.toLowerCase() !== 'content-type') sent[key] = base[key]
    }
    sent['Content-Type'] = 'application/json'
    const init = { method: 'POST', headers: sent, signal: call.signal, body }
    if (call.expired()) return { failure: call.failure(null) }
    const callFetch = async (input, budgetInit) => {
      budgetInit?.signal?.addEventListener?.('abort', () => call.halt('timeout'), { once: true })
      const raw = Promise.resolve().then(() => fetchImpl(input, { ...budgetInit, signal: call.signal }))
      const settled = await call.race(() => raw)
      if (settled === STOPPED) {
        // The helper's finally must run now, so its timer and listener are released with the call.
        raw.then((late) => releaseBody(late), () => {})
        throw new Error('tracker row call stopped')
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
        return { failure: refuse(null, 'TRACKER_ROWS_CLIENT_TIMEOUT', true) }
      }
      if (call.reason() === 'aborted' || signal?.aborted === true) {
        return { failure: refuse(null, 'TRACKER_ROWS_CLIENT_ABORTED', false) }
      }
      return { failure: refuse(null, 'TRACKER_ROWS_CLIENT_NETWORK', true) }
    }
    const response = outcome.value
    if (onResponse) {
      const authorizationKey = Object.keys(sent).find((key) => key.toLowerCase() === 'authorization')
      try {
        const observed = onResponse(response, url, authorizationKey === undefined ? undefined : sent[authorizationKey])
        if (observed && typeof observed.then === 'function') observed.then(undefined, () => {})
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
      return { failure: refuse(null, 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID', false) }
    }
    if (status === 401) {
      releaseBody(response)
      return { failure: refuse(401, 'UNAUTHENTICATED', false) }
    }
    if (status === 200 || status === 201) return { response, status }
    try {
      return { failure: await refusalOf(call, response, status) }
    } catch {
      return { failure: refuse(status, 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID', status >= 500) }
    }
  }

  async function createTrackerRows(options) {
    let call
    let signal
    let startedAt
    let status = null
    let success
    // Every refusal is decided when it leaves: a caller abort or a spent budget at that moment
    // makes it the call's stop failure. It runs before cleanup, so cleanup cannot change it.
    const settle = (answer) => {
      try {
        if (call === undefined) {
          if (isSignal(signal) && signal?.aborted === true) return refuse(answer.status, 'TRACKER_ROWS_CLIENT_ABORTED', false)
          if (startedAt !== undefined && monotonicNow() - startedAt >= budget) {
            return refuse(answer.status, 'TRACKER_ROWS_CLIENT_TIMEOUT', true)
          }
          return answer
        }
        try {
          if (signal?.aborted === true) call.halt('aborted')
        } catch {
          // A hostile signal getter cannot replace the answer.
        }
        return call.expired() ? call.failure(answer.status) : answer
      } catch {
        return answer
      }
    }
    try {
      startedAt = monotonicNow()
      if (!isPlainObject(options) || !hasOnlyKeys(options, OPTION_KEYS)) {
        return settle(refuse(null, 'TRACKER_ROWS_CLIENT_REQUEST_INVALID', false))
      }
      const { drawingId, projectId = null, drawingUnits, request } = options
      signal = options.signal
      if (!isDrawingId(drawingId)) return settle(refuse(null, 'TRACKER_ROWS_REQUEST_INVALID', false))
      if (projectId !== null && !isProjectId(projectId)) return settle(refuse(null, 'TRACKER_ROWS_PROJECT_ID_INVALID', false))
      if (!isSignal(signal)) return settle(refuse(null, 'TRACKER_ROWS_CLIENT_REQUEST_INVALID', false))
      call = openCall(budget, signal, startedAt)
      if (call.expired()) return call.failure(null)
      const validated = validateTrackerRowsRequest(request, { drawingUnits })
      if (!validated.ok) return settle(refuse(null, validated.code, false))
      const snapshot = validated.body
      const body = JSON.stringify(snapshot)
      const query = projectId === null ? '' : `?project_id=${encodeURIComponent(projectId)}`
      const url = `${apiBase}/api/drawings/${encodeURIComponent(drawingId)}/tracker-rows${query}`
      if (call.expired()) return call.failure(null)
      const exchanged = await exchange(call, { drawingId, url, body, signal })
      if (exchanged.failure) return settle(exchanged.failure)
      status = exchanged.status
      let read
      try {
        read = await readBoundedJson(call, exchanged.response)
      } catch {
        read = UNREADABLE
      }
      if (read === STOPPED) return call.failure(status)
      const value = read.ok ? validateTrackerRowsResult(read.value, {
        drawingId, projectId, drawingUnits, request: snapshot, status,
      }) : null
      if (value === null) return settle(refuse(status, 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID', false))
      success = { ok: true, status, value }
    } catch {
      return settle(refuse(status, status === null ? 'TRACKER_ROWS_CLIENT_REQUEST_INVALID' : 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID', false))
    } finally {
      call?.close()
    }
    // Cleanup is inside the success budget. A refusal returned above stays decided.
    try {
      if (signal?.aborted === true) call.halt('aborted')
    } catch {
      // A hostile signal getter cannot replace the answer.
    }
    return call.expired() ? call.failure(status) : success
  }

  return Object.freeze({ createTrackerRows })
}
