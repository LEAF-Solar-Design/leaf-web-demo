import { createCatalogToolSnapshot, createRunSubmissionRequest } from '../runIntent.js'
import { createSolarImportClient } from './solarImportClient.js'
import {
  buildPvcaseParams, validatePvcaseSource, validatePvcaseCommit, validatePvcaseExport,
  validatePvcaseCommittedGraph, pvcaseDrawingId, pvcaseProjectId, pvcaseInteger,
  pvcaseFailure, pvcaseServerRefusalCode, pvcaseAcceptedCommit,
} from './solarPvcaseModel.js'

export const PVCASE_INTAKE_RESPONSE_MAX_BYTES = 33_554_432
export const PVCASE_SOURCE_MAX_BYTES = 16_777_216
const STOPPED = Symbol('stopped')
const now = () => globalThis.performance?.now?.() ?? Date.now()
const jobIdValid = (id) => typeof id === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(id)
const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)
const only = (v, keys) => object(v) && Reflect.ownKeys(v).every((k) => keys.includes(k))
const positiveVersion = (v) => Number.isSafeInteger(v) && v > 0
const budgetValid = (v) => pvcaseInteger(v, 1, 600000)
function release(response, reader) {
  try {
    const p = reader ? reader.cancel() : response?.body?.cancel?.()
    Promise.resolve(p).catch(() => {})
  } catch { /* Cleanup does not replace a verified result. */ }
  try { reader?.releaseLock?.() } catch { /* A pending read can retain its lock. */ }
}
function openCall(timeout, started) {
  const controller = new AbortController()
  let why = null
  let wake
  let caller
  const stopped = new Promise((resolve) => { wake = resolve })
  const halt = (reason) => {
    if (why !== null) return
    why = reason; wake(STOPPED)
    try { controller.abort() } catch { /* The race also stops observation. */ }
  }
  const onAbort = () => halt('aborted')
  const timer = setTimeout(() => halt('timeout'), Math.max(0, timeout - (now() - started)))
  return {
    signal: controller.signal,
    attach(signal) {
      if (signal == null) return
      if (typeof signal.aborted !== 'boolean' || typeof signal.addEventListener !== 'function' || typeof signal.removeEventListener !== 'function') throw new TypeError('Invalid signal')
      caller = signal
      if (signal.aborted) halt('aborted')
      else signal.addEventListener('abort', onAbort, { once: true })
    },
    expired() {
      if (why !== null) return true
      if (now() - started >= timeout) halt('timeout')
      return why !== null
    },
    race(work) { return Promise.race([Promise.resolve().then(work).then((value) => ({ value }), () => ({ failed: true })), stopped]) },
    failure(status = null) { return pvcaseFailure(why ?? 'timeout', status, why !== 'aborted') },
    close() {
      clearTimeout(timer)
      try { caller?.removeEventListener('abort', onAbort) } catch { /* Cleanup is contained. */ }
      try { if (caller?.aborted) halt('aborted') } catch { /* A hostile signal is contained. */ }
    },
  }
}
async function bounded(timeout, work, started = now()) {
  const call = openCall(timeout, started)
  let result
  let knownJob
  const retain = (id) => { knownJob = id }
  try { result = await work(call, retain) } catch { result = pvcaseFailure('response') }
  finally { call.close() }
  if (result?.ok && call.expired()) result = call.failure(result.status)
  if (!result || typeof result.ok !== 'boolean') result = pvcaseFailure('response')
  if (!result.ok && knownJob) result = { ...result, jobId: knownJob }
  return result
}

function refusal(body, status) {
  let codes
  try { codes = [body?.reason_code, body?.error?.reason_code, body?.error?.error_code] } catch { return pvcaseFailure('refused', status) }
  const valid = codes.filter((c) => typeof c === 'string' && c.length <= 64 && (/^[A-Z][A-Z0-9_]*$/.test(c) || /^[a-z][a-z0-9_]*$/.test(c)))
  const code = valid.map(pvcaseServerRefusalCode).find(Boolean)
    ?? (valid.some((c) => ['admitted', 'converted', 'solved', 'exported', 'downloaded'].includes(c)) ? 'refused'
      : status === 401 ? 'signIn' : status === 403 ? 'forbidden' : 'refused')
  return pvcaseFailure(code, status, ['PVS_WRITES_DRAINED', 'PVS_STORE_UNAVAILABLE'].includes(code?.toUpperCase()))
}
async function readJson(call, response, cap) {
  let reader
  try {
    const length = response.headers?.get?.('content-length')
    if (typeof length === 'string' && /^\d+$/.test(length.trim()) && Number(length) > cap) return pvcaseFailure('response', response.status)
    if (typeof response.body?.getReader !== 'function') return pvcaseFailure('response', response.status)
    reader = response.body.getReader()
    const chunks = []
    let total = 0
    for (;;) {
      if (call.expired()) return call.failure(response.status)
      const step = await call.race(() => reader.read())
      if (step === STOPPED || call.expired()) return call.failure(response.status)
      if (step.failed || !object(step.value)) return pvcaseFailure('response', response.status)
      if (step.value.done === true) break
      const bytes = step.value.value
      if (Object.prototype.toString.call(bytes) !== '[object Uint8Array]') return pvcaseFailure('response', response.status)
      total += bytes.byteLength
      if (total > cap) return pvcaseFailure('response', response.status)
      chunks.push(bytes)
    }
    const bytes = new Uint8Array(total)
    let at = 0
    for (const chunk of chunks) { bytes.set(chunk, at); at += chunk.byteLength }
    const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))
    return { ok: true, status: response.status, value }
  } catch { return pvcaseFailure('response', response?.status ?? null) }
  finally { release(response, reader) }
}
function checkTransport(transport) {
  if (!object(transport) || typeof transport.fetchImpl !== 'function' || typeof transport.headers !== 'function'
    || typeof (transport.apiBase ?? '') !== 'string' || (transport.apiBase ?? '').length > 2048
    || (transport.onResponse !== undefined && typeof transport.onResponse !== 'function')) throw new TypeError('Invalid transport')
  return transport
}
function echoesMatch(body, context) {
  return [['tenant_id', context.tenantId], ['org_id', context.orgId], ['organization_id', context.orgId], ['project_id', context.projectId], ['drawing_id', context.drawingId]]
    .every(([key, value]) => !Object.hasOwn(body, key) || body[key] === value)
}
async function exchange(call, transport, drawingId, path, cap, init = {}) {
  let response
  let status = null
  try {
    if (call.expired()) return call.failure()
    const fresh = transport.headers(drawingId)
    if (!object(fresh)) return pvcaseFailure('request')
    const headers = Object.create(null)
    const forced = new Set(Object.keys(init.headers ?? {}).map((key) => key.toLowerCase()))
    for (const key of Reflect.ownKeys(fresh)) {
      if (typeof key !== 'string' || typeof fresh[key] !== 'string') return pvcaseFailure('request')
      if (!forced.has(key.toLowerCase()) && key.toLowerCase() !== 'x-checkout-capability') headers[key] = fresh[key]
    }
    Object.assign(headers, init.headers ?? {})
    if (call.expired()) return call.failure()
    const pending = Promise.resolve().then(() => transport.fetchImpl(`${transport.apiBase ?? ''}${path}`, { ...init, method: init.method ?? 'GET', headers, signal: call.signal }))
    const got = await call.race(() => pending)
    if (got === STOPPED || call.expired()) { pending.then((late) => release(late), () => {}); return call.failure() }
    if (got.failed) return pvcaseFailure('network', null, true)
    response = got.value
    status = response?.status
    if (!Number.isInteger(status) || status < 100 || status > 599) return pvcaseFailure('response')
    try { Promise.resolve(transport.onResponse?.(response, path, headers.Authorization ?? headers.authorization)).catch(() => {}) } catch { /* Observers cannot change acceptance. */ }
    const read = await readJson(call, response, cap)
    if (!read.ok) return read
    if (call.expired()) return call.failure(status)
    return status >= 200 && status < 300 ? read : refusal(read.value, status)
  } catch { release(response); return pvcaseFailure('response', status) }
}

export async function readCommittedIntake(options) {
  const started = now()
  try {
    if (!only(options, ['drawingId', 'version', 'signal', 'timeoutMs', 'transport'])) return pvcaseFailure('request')
    const { drawingId, version, signal, timeoutMs = 120000, transport } = options
    if (!pvcaseDrawingId(drawingId) || !positiveVersion(version) || !budgetValid(timeoutMs)) return pvcaseFailure('request')
    checkTransport(transport)
    return bounded(timeoutMs, async (call) => {
      call.attach(signal)
      const read = await exchange(call, transport, drawingId, `/api/drawings/${drawingId}/intake?version=${version}`, PVCASE_INTAKE_RESPONSE_MAX_BYTES)
      if (!read.ok) return read
      if (read.status !== 200 || !object(read.value) || !object(read.value.intake) || read.value.version !== version
        || (Object.hasOwn(read.value, 'error') && read.value.error !== null)
        || (Object.hasOwn(read.value, 'degraded_mode') && typeof read.value.degraded_mode !== 'boolean')) return pvcaseFailure('response', read.status)
      return read
    }, started)
  } catch { return pvcaseFailure('request') }
}

// Bytes are measured through the intrinsic getters, so a Uint8Array or ArrayBuffer made in another realm (an
// iframe, a test environment) is read the same way, and an object that only claims the Uint8Array tag is refused.
const TYPED_ARRAY_PROTOTYPE = Object.getPrototypeOf(Uint8Array.prototype)
const typedArrayTag = Object.getOwnPropertyDescriptor(TYPED_ARRAY_PROTOTYPE, Symbol.toStringTag).get
const typedArrayByteLength = Object.getOwnPropertyDescriptor(TYPED_ARRAY_PROTOTYPE, 'byteLength').get
const arrayBufferByteLength = Object.getOwnPropertyDescriptor(ArrayBuffer.prototype, 'byteLength').get
const blobSize = typeof Blob === 'function' ? Object.getOwnPropertyDescriptor(Blob.prototype, 'size')?.get : null
function sourceSize(file) {
  try { if (blobSize) return blobSize.call(file) } catch { /* Try the remaining intrinsic brands. */ }
  try { if (typedArrayTag.call(file) === 'Uint8Array') return typedArrayByteLength.call(file) } catch { /* Try ArrayBuffer next. */ }
  try { return arrayBufferByteLength.call(file) } catch { return null }
}

export function createSolarPvcaseClient({ fetchImpl, apiBase = '', headers, onResponse, runOptions = {} } = {}) {
  const transport = checkTransport({ fetchImpl, apiBase, headers, onResponse })
  if (!only(runOptions, ['timeoutMs', 'uploadTimeoutMs', 'intakeTimeoutMs']) || Object.values(runOptions).some((v) => !budgetValid(v))) throw new TypeError('Invalid deadlines')
  const runBudget = runOptions.timeoutMs ?? 120000
  const uploadBudget = runOptions.uploadTimeoutMs ?? 120000
  const intakeBudget = runOptions.intakeTimeoutMs ?? 120000
  const downloader = createSolarImportClient({ fetchImpl, apiBase, headers, onResponse })

  async function uploadSource(options) {
    return bounded(uploadBudget, async (call) => {
      if (!only(options, ['drawingId', 'projectId', 'file', 'signal'])) return pvcaseFailure('request')
      const { drawingId, projectId = null, file, signal } = options
      if (!pvcaseDrawingId(drawingId)) return pvcaseFailure('drawing')
      if (!pvcaseProjectId(projectId)) return pvcaseFailure('project')
      const size = sourceSize(file)
      if (size === null) return pvcaseFailure('fileRequired')
      if (size < 1) return pvcaseFailure('PVG_INVALID_JSON')
      if (size > PVCASE_SOURCE_MAX_BYTES) return pvcaseFailure('PVG_INPUT_BYTES_EXCEEDED')
      call.attach(signal)
      const result = await exchange(call, transport, drawingId, `/api/drawings/${drawingId}/imports/pvcase-g33${projectId === null ? '' : `?project_id=${encodeURIComponent(projectId)}`}`, 65536, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: file })
      if (!result.ok) return result
      if (result.status !== 200) return pvcaseFailure('response', result.status)
      const valid = validatePvcaseSource(result.value, { drawingId, projectId })
      return { ...valid, status: result.status }
    })
  }
  async function observe(call, retain, binding, jobId) {
    retain(jobId)
    for (;;) {
      const result = await exchange(call, transport, binding.context.drawingId, `/api/jobs/${jobId}`, 1048576)
      if (!result.ok) return result
      const record = result.value
      if (result.status !== 200 || !object(record) || record.job_id !== jobId
        || !echoesMatch(record, binding.context)
        || (record.tool !== undefined && record.tool !== binding.tool)
        || (record.dwg !== undefined && record.dwg !== binding.context.drawingId)
        || (record.dwg_version !== undefined && record.dwg_version !== binding.context.drawingVersion)
        || !['submitted', 'queued', 'running', 'complete', 'failed'].includes(record.status)) return pvcaseFailure('response', result.status)
      if (record.status === 'failed') {
        if (record.result == null) return object(record.error) ? refusal(record, result.status) : pvcaseFailure('response', result.status)
        if (!object(record.result) || record.result.ok !== false || record.result.tool !== binding.tool) return pvcaseFailure('response', result.status)
        return refusal(record.result, result.status)
      }
      if (record.status === 'complete') {
        if (!object(record.result)) return pvcaseFailure('response', result.status)
        if (record.result.ok === false) return refusal(record.result, result.status)
        const valid = binding.operation === 'export' ? validatePvcaseExport(record.result, { ...binding, jobId }) : validatePvcaseCommit(record.result, { ...binding, jobId })
        return { ...valid, status: result.status }
      }
      let timer
      try {
        const paused = await call.race(() => new Promise((resolve) => { timer = setTimeout(resolve, 1000) }))
        if (paused === STOPPED || call.expired()) return call.failure(result.status)
      } finally { clearTimeout(timer) }
    }
  }
  function run(operation, options, resume = false) {
    return bounded(runBudget, async (call, retain) => {
      if (!only(options, resume ? ['context', 'source', 'catalogRow', 'signal', 'operation', 'jobId'] : ['context', 'source', 'catalogRow', 'signal'])) return pvcaseFailure('request')
      const { context, source, catalogRow, signal } = options
      const chosen = resume ? options.operation : operation
      if (!['convert', 'solve', 'export'].includes(chosen)) return pvcaseFailure('request')
      const tool = `solar-pvcase-${chosen}`
      let snapshot
      try {
        snapshot = createCatalogToolSnapshot(catalogRow)
        if (snapshot.name !== tool || typeof snapshot.catalogDigest !== 'string' || !/^[0-9a-f]{64}$/.test(snapshot.catalogDigest)) return pvcaseFailure('catalog')
      } catch { return pvcaseFailure('catalog') }
      const params = buildPvcaseParams(chosen, context, source)
      if (!params.ok) return params
      const captured = { tenantId: context.tenantId, orgId: context.orgId ?? null, projectId: context.projectId ?? null, drawingId: context.drawingId, drawingVersion: context.drawingVersion, graphRev: context.graphRev }
      const admitted = validatePvcaseSource(source, captured)
      if (!admitted.ok) return admitted
      const binding = { context: captured, source: admitted.value, operation: chosen, tool }
      call.attach(signal)
      if (resume) {
        if (!jobIdValid(options.jobId)) return pvcaseFailure('request')
        return observe(call, retain, binding, options.jobId)
      }
      if (chosen !== 'export' && (typeof context.checkoutCapability !== 'string' || !context.checkoutCapability)) return pvcaseFailure('checkout')
      const request = createRunSubmissionRequest(tool, params.value, captured.drawingId, {
        orgId: captured.orgId, projectId: captured.projectId,
        checkoutCapability: chosen === 'export' ? undefined : context.checkoutCapability,
        catalogDigest: snapshot.catalogDigest, dwgVersion: captured.drawingVersion,
      })
      const body = JSON.stringify(request.body)
      if (new TextEncoder().encode(body).byteLength > 4096) return pvcaseFailure('request')
      const submitted = await exchange(call, transport, captured.drawingId, '/api/run', 1048576, { method: 'POST', headers: { ...request.headers, 'Content-Type': 'application/json' }, body })
      if (!submitted.ok) return submitted
      if (submitted.status !== 202 || submitted.value?.status !== 'submitted' || !jobIdValid(submitted.value.job_id)
        || !echoesMatch(submitted.value, captured) || (submitted.value.error !== undefined && submitted.value.error !== null)
        || (submitted.value.degraded_mode !== undefined && typeof submitted.value.degraded_mode !== 'boolean')) return pvcaseFailure('response', submitted.status)
      return observe(call, retain, binding, submitted.value.job_id)
    })
  }
  async function readCommittedGraph(options) {
    return bounded(intakeBudget, async (call) => {
      if (!only(options, ['context', 'receipt', 'signal'])) return pvcaseFailure('countsUnavailable')
      const { context, receipt, signal } = options
      if (!pvcaseAcceptedCommit(receipt, context)) return pvcaseFailure('countsUnavailable')
      call.attach(signal)
      const read = await readCommittedIntake({ drawingId: context.drawingId, version: receipt.new_version.version, signal: call.signal, timeoutMs: intakeBudget, transport })
      if (!read.ok) return pvcaseFailure('countsUnavailable', read.status, read.retryable)
      return { ...validatePvcaseCommittedGraph(read.value, { context, receipt }), status: read.status }
    }).then((r) => r.ok ? r : { ...r, code: 'countsUnavailable' })
  }
  async function download(options) {
    try { return await downloader.downloadArtifact({ ...options, current: false }) } catch { return pvcaseFailure('artifactInvalid') }
  }
  return Object.freeze({ uploadSource, convert: (options) => run('convert', options), solve: (options) => run('solve', options), exportAssignments: (options) => run('export', options), observeJob: (options) => run(null, options, true), readCommittedGraph, download })
}
