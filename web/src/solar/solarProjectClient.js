import { createRunSubmissionRequest, projectSolarIntentExtras, projectSolarRequestExtras } from '../runIntent.js'
import { createSolarDrawingBinding, sameSolarDrawingBinding, sameSolarCheckoutBinding } from './solarDrawingBinding.js'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const HEX = /^[0-9a-f]{64}$/
const DIGEST = /^sha256:[0-9a-f]{64}$/
const TOOL = /^[a-z0-9][a-z0-9-]{0,127}$/
const CAPABILITY = /^lco1\.[0-9a-f]{64}$/
const CONTAINS_CAPABILITY = /lco1\.[0-9a-f]{64}/
// A server reason is a closed code; anything else (prose, a dot, a capability) reads as HTTP_ERROR.
const REASON = /^[A-Za-z][A-Za-z0-9_]{0,127}$/
const MAX_RESPONSE_DEPTH = 128
const MAX_BODY = 262144
const MAX_ARTIFACT = 16777216
const REF_KEYS = ['organization_id', 'project_id', 'drawing_id', 'source_version_id',
  'source_intake_sha256', 'graph_sha256', 'artifact_id', 'media_type', 'filename',
  'byte_length', 'content_sha256', 'schema', 'download']
const local = (reason) => ({ ok: false, status: null, reason, envelope: null })
const utf8 = (value) => new TextEncoder().encode(value).byteLength
const plain = (value) => value !== null && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype
const assert = (condition) => { if (!condition) throw new TypeError('Invalid Solar request') }

function copyJson(value, seen = new Set()) {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') {
    assert(typeof value !== 'string' || !CONTAINS_CAPABILITY.test(value))
    return value
  }
  if (typeof value === 'number') { assert(Number.isFinite(value)); return Object.is(value, -0) ? 0 : value }
  assert((Array.isArray(value) || plain(value)) && !seen.has(value))
  seen.add(value)
  const keys = Reflect.ownKeys(value)
  const result = Array.isArray(value) ? [] : {}
  if (Array.isArray(value)) {
    assert(keys.length === value.length + 1)
    for (let i = 0; i < value.length; i += 1) {
      const descriptor = Object.getOwnPropertyDescriptor(value, String(i))
      assert(descriptor && Object.hasOwn(descriptor, 'value'))
      result.push(copyJson(descriptor.value, seen))
    }
  } else {
    assert(keys.every((key) => typeof key === 'string'))
    for (const key of keys.sort()) {
      assert(!/authorization|capability|bearer|access[_-]?token/i.test(key) && !CONTAINS_CAPABILITY.test(key))
      const descriptor = Object.getOwnPropertyDescriptor(value, key)
      assert(descriptor && Object.hasOwn(descriptor, 'value') && descriptor.enumerable)
      Object.defineProperty(result, key, { value: copyJson(descriptor.value, seen), enumerable: true })
    }
  }
  seen.delete(value)
  return Object.freeze(result)
}

function parameters(value, binding) {
  assert(plain(value))
  const copied = copyJson(value)
  assert(!Object.hasOwn(copied, 'drawing_id') || copied.drawing_id === binding.drawing_id)
  return copyJson({ ...copied, drawing_id: binding.drawing_id })
}

function matches(data, binding, version = true) {
  return data?.organization_id === binding.organization_id && data?.project_id === binding.project_id &&
    data?.drawing_id === binding.drawing_id && (!version || data?.input_version_id === binding.input_version_id)
}

function validCheckout(data, binding) {
  const lease = data?.checkout
  return matches(data, binding, false) && (lease === null ||
    (plain(lease) && typeof lease.holder === 'string' && typeof lease.fence === 'string' &&
      /^[0-9]+$/.test(lease.fence) && Number.isFinite(Date.parse(lease.acquired)) &&
      Number.isFinite(Date.parse(lease.expires))))
}

// Reads a parsed response body exactly once into frozen plain data. Each property value is read
// one time, so nothing a caller-visible value does can run after the scope checks that follow;
// a non-plain prototype, a non-finite number or a tree deeper than MAX_RESPONSE_DEPTH is refused.
function responseSnapshot(value, depth = 0) {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return value
  if (typeof value === 'number') { assert(Number.isFinite(value)); return value }
  assert(depth < MAX_RESPONSE_DEPTH)
  if (Array.isArray(value)) {
    assert(Object.getPrototypeOf(value) === Array.prototype)
    const length = value.length
    const result = []
    for (let i = 0; i < length; i += 1) result.push(responseSnapshot(value[i], depth + 1))
    return Object.freeze(result)
  }
  assert(plain(value))
  const result = {}
  for (const key of Object.keys(value)) {
    Object.defineProperty(result, key, { value: responseSnapshot(value[key], depth + 1), enumerable: true })
  }
  return Object.freeze(result)
}

const reasonOf = (envelope) => {
  const reason = envelope?.error?.reason_code || envelope?.reason_code
  return typeof reason === 'string' && REASON.test(reason) ? reason : 'HTTP_ERROR'
}

// Never expose transport credentials through outcomes or external-store state.
function publicData(value) {
  if (typeof value === 'string' && CONTAINS_CAPABILITY.test(value)) return null
  if (Array.isArray(value)) return Object.freeze(value.map(publicData))
  if (!plain(value)) return value
  const result = {}
  for (const [key, child] of Object.entries(value)) {
    if (CONTAINS_CAPABILITY.test(key) || ['checkout_capability', 'x-checkout-capability', 'authorization'].includes(key.toLowerCase()) ||
        (typeof child === 'string' && CONTAINS_CAPABILITY.test(child))) continue
    Object.defineProperty(result, key, { value: publicData(child), enumerable: true })
  }
  return Object.freeze(result)
}

export function createSolarProjectClient({ fetchImpl = (...args) => globalThis.fetch(...args),
  apiBase = '', authHeaders = () => ({}), onUnauthorized, timeoutMs = 30000 } = {}) {
  assert(typeof fetchImpl === 'function' && typeof authHeaders === 'function' && typeof apiBase === 'string' &&
    typeof timeoutMs === 'number' && Number.isFinite(timeoutMs) && timeoutMs > 0)
  let binding = null
  let authorityKey = null
  let bindingGeneration = 0
  let checkoutGeneration = 0
  let leaseRevision = 0
  let capability = null
  let checkout = null
  let pending = false
  let error = null
  let disposed = false
  let expiryTimer = null
  let mutation = null
  const listeners = new Set()
  const operations = new Set()
  let snapshot
  const publish = () => {
    snapshot = Object.freeze({ binding, authorityKey, checkout, ownsCheckout: !!capability,
      pending, error, leaseRevision })
    for (const listener of listeners) { try { listener() } catch { /* observer isolation */ } }
  }
  const abort = (predicate) => {
    for (const operation of operations) if (predicate(operation)) operation.controller.abort()
  }
  const rotate = (token, lease = checkout) => {
    clearTimeout(expiryTimer)
    expiryTimer = null
    capability = token
    checkout = publicData(lease)
    leaseRevision += 1
    abort((operation) => operation.preview)
    if (token) {
      const scheduleExpiry = () => {
        const remaining = Date.parse(lease.expires) - Date.now()
        if (remaining <= 0) rotate(null)
        else expiryTimer = setTimeout(scheduleExpiry, Math.min(remaining, 2147483647))
      }
      if (Date.parse(lease.expires) <= Date.now()) { capability = null; leaseRevision += 1 }
      else scheduleExpiry()
    }
    publish()
  }
  const capture = (checkoutScope = false, preview = false) => ({ binding, authorityKey,
    generation: checkoutScope ? checkoutGeneration : bindingGeneration, checkoutScope, preview,
    revision: leaseRevision })
  const current = (scope) => !disposed && !!binding && scope.authorityKey === authorityKey &&
    scope.generation === (scope.checkoutScope ? checkoutGeneration : bindingGeneration) &&
    (!scope.preview || scope.revision === leaseRevision)

  async function request(path, { method = 'GET', body, headers = {}, signal,
    scope = capture(), validate, consume } = {}) {
    if (!scope.binding) return local('SIP_R7_BINDING_INVALID')
    if (!current(scope)) return local('SIP_R7_SCOPE_STALE')
    if (signal !== undefined && (!signal || typeof signal.aborted !== 'boolean' ||
        typeof signal.addEventListener !== 'function' || typeof signal.removeEventListener !== 'function')) {
      return local('SIP_R7_REQUEST_INVALID')
    }
    const controller = new AbortController()
    const operation = { ...scope, controller }
    operations.add(operation)
    let timedOut = false
    const callerAbort = () => controller.abort()
    signal?.addEventListener('abort', callerAbort, { once: true })
    if (signal?.aborted) controller.abort()
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
    let rejectAbort
    const aborted = new Promise((resolve, reject) => { rejectAbort = reject })
    const rejectInterrupted = () => rejectAbort(new Error('Solar operation interrupted'))
    controller.signal.addEventListener('abort', rejectInterrupted, { once: true })
    const wait = (promise) => Promise.race([promise, aborted])
    const interrupted = () => !current(scope) ? local('SIP_R7_SCOPE_STALE') :
      timedOut ? local('SIP_R7_TIMEOUT') : controller.signal.aborted ? local('SIP_R7_ABORTED') : null
    let consuming = false
    try {
      if (interrupted()) return interrupted()
      const b = scope.binding
      const sentHeaders = { ...authHeaders(), 'X-Org-Id': b.organization_id,
        'X-Project-Id': b.project_id, 'X-Tenant-Id': b.organization_id,
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...headers }
      const response = await wait(fetchImpl(`${apiBase}${path}`, { method, headers: sentHeaders,
        cache: 'no-store', signal: controller.signal, ...(body !== undefined ? { body } : {}) }))
      if (interrupted()) return interrupted()
      consuming = true
      if (!response.ok && response.status !== 304) {
        const envelope = await wait(response.json().then((body) => responseSnapshot(body)).catch(() => null))
        if (interrupted()) return interrupted()
        if (response.status === 401) {
          try { onUnauthorized?.(response, sentHeaders.Authorization ?? sentHeaders.authorization) }
          catch { /* observer isolation */ }
        }
        if (interrupted()) return interrupted()
        return { ok: false, status: response.status, reason: reasonOf(envelope), envelope: publicData(envelope) }
      }
      const data = await wait(consume ? consume(response, interrupted)
        : response.json().then((body) => responseSnapshot(body)).catch(() => { throw new TypeError('Invalid Solar response') }))
      if (interrupted()) return interrupted()
      if (validate && !validate(data, scope.binding)) return local('SIP_R7_RESPONSE_INVALID')
      return { ok: true, status: response.status, data: consume ? data : publicData(data) }
    } catch (failure) {
      return interrupted() || local(consuming ? 'SIP_R7_RESPONSE_INVALID' : 'SIP_R7_TRANSPORT_FAILED')
    } finally {
      clearTimeout(timer)
      controller.signal.removeEventListener('abort', rejectInterrupted)
      signal?.removeEventListener('abort', callerAbort)
      operations.delete(operation)
    }
  }

  const guarded = (action, checkoutScope = false, preview = false) => {
    if (!binding || disposed) return Promise.resolve(local('SIP_R7_BINDING_INVALID'))
    const scope = capture(checkoutScope, preview)
    try { return Promise.resolve(action(scope))
      .catch(() => local('SIP_R7_REQUEST_INVALID'))
      .then((outcome) => current(scope) ? outcome : local('SIP_R7_SCOPE_STALE')) }
    catch { return Promise.resolve(local('SIP_R7_REQUEST_INVALID')) }
  }
  const checkoutPath = (b) => `/api/projects/${b.project_id}/drawings/${b.drawing_id}/checkout`
  const proof = () => capability ? { 'X-Checkout-Capability': capability } : {}
  const checkIntent = (intent) => {
    assert(plain(intent))
    const extras = projectSolarIntentExtras(Object.getOwnPropertyDescriptor(intent, 'tool')?.value, intent)
    assert(Reflect.ownKeys(intent).length === 6 + Object.keys(extras).length &&
      ['binding', 'authorityKey', 'tool', 'params', 'catalogDigest', 'idempotencyKey'].every((key) => {
        const descriptor = Object.getOwnPropertyDescriptor(intent, key)
        return descriptor && Object.hasOwn(descriptor, 'value')
      }))
    assert(intent && sameSolarDrawingBinding(intent.binding, binding) && intent.authorityKey === authorityKey)
    const normalized = prepareRun(intent)
    assert(JSON.stringify(normalized.params) === JSON.stringify(copyJson(intent.params)))
    return normalized
  }
  function prepareRun(request) {
    const { tool, params, catalogDigest, idempotencyKey } = request
    const extras = projectSolarIntentExtras(tool, request)
    assert(binding && !disposed && typeof tool === 'string' && TOOL.test(tool) &&
      typeof catalogDigest === 'string' && DIGEST.test(catalogDigest) &&
      typeof idempotencyKey === 'string' && idempotencyKey.trim().length > 0 && utf8(idempotencyKey) <= 256 && !CONTAINS_CAPABILITY.test(idempotencyKey))
    return Object.freeze({ binding, authorityKey, tool, params: parameters(params, binding), catalogDigest, idempotencyKey, ...extras })
  }
  async function mutate(kind, options, scope) {
    if (!binding || disposed) return local('SIP_R7_BINDING_INVALID')
    if (mutation) return local('SIP_R7_CHECKOUT_BUSY')
    if (!plain(options)) return local('SIP_R7_REQUEST_INVALID')
    let body
    try {
      if (kind === 'take') {
        const { holder, ttlS = 3600 } = options
        assert(typeof ttlS === 'number' && Number.isFinite(ttlS) && ttlS > 0 && ttlS <= 86400)
        assert(holder === undefined || (typeof holder === 'string' && Array.from(holder).length >= 1 && Array.from(holder).length <= 200 && !CONTAINS_CAPABILITY.test(holder)))
        body = JSON.stringify({ ...(holder !== undefined ? { holder } : {}), ttl_s: ttlS })
      }
    } catch { return local('SIP_R7_REQUEST_INVALID') }
    const lock = {}
    mutation = lock
    pending = true
    error = null
    publish()
    const outcome = await request(checkoutPath(scope.binding), { scope, signal: options.signal,
      method: kind === 'take' ? 'POST' : 'DELETE', body, headers: proof(),
      validate: (data, b) => validCheckout(data, b) && (kind === 'take'
        ? data.acquired === true && data.checkout !== null && CAPABILITY.test(data.checkout_capability)
        : typeof data.released === 'boolean' && data.checkout === null),
      consume: async (response) => responseSnapshot(await response.json()) })
    if (!current(scope)) {
      if (mutation === lock) { mutation = null; pending = false; publish() }
      return local('SIP_R7_SCOPE_STALE')
    }
    if (current(scope)) {
      if (outcome.ok) {
        rotate(kind === 'take' ? outcome.data.checkout_capability : null, outcome.data.checkout)
        outcome.data = publicData(outcome.data)
      } else error = outcome.reason
    }
    if (mutation === lock) { mutation = null; pending = false; publish() }
    return current(scope) ? outcome : local('SIP_R7_SCOPE_STALE')
  }

  const client = {
    setScope(value, key) {
      let next = null
      if (value !== null) {
        next = createSolarDrawingBinding(value)
        assert(typeof key === 'string' && utf8(key) >= 1 && utf8(key) <= 256)
      } else assert(key === null)
      assert(!disposed)
      if ((next === null && binding === null) || (sameSolarDrawingBinding(next, binding) && key === authorityKey)) return
      const checkoutChanged = !sameSolarCheckoutBinding(next, binding) || key !== authorityKey
      bindingGeneration += 1
      if (checkoutChanged) checkoutGeneration += 1
      abort((operation) => !operation.checkoutScope || checkoutChanged)
      binding = next
      authorityKey = key
      error = null
      if (checkoutChanged) { checkout = null; rotate(null, null) }
      else publish()
    },
    getSnapshot: () => snapshot,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    readContext: (options = {}) => guarded((scope) => {
      assert(plain(options))
      const { signal } = options
      return request(
      `/api/projects/${binding.project_id}/drawing-versions/${binding.input_version_id}/context?drawing_id=${binding.drawing_id}`,
      { scope, signal, validate: (data, b) => matches(data?.context, b) && UUID.test(data.context.head_version_id) && typeof data.context.is_head === 'boolean' })
    }),
    readCheckout: (options = {}) => guarded(async (scope) => {
      assert(plain(options))
      const { signal } = options
      const outcome = await request(checkoutPath(scope.binding), { scope, signal, validate: validCheckout })
      if (outcome.ok && current(scope)) {
        error = null
        const lease = outcome.data.checkout
        if (capability && (!lease || lease.fence !== checkout?.fence || Date.parse(lease.expires) <= Date.now())) rotate(null, lease)
        else { checkout = lease; publish() }
      }
      return current(scope) ? outcome : local('SIP_R7_SCOPE_STALE')
    }, true),
    acquireCheckout: (options = {}) => guarded((scope) => mutate('take', options, scope), true),
    releaseCheckout: (options = {}) => guarded((scope) => mutate('release', options, scope), true),
    prepareRun,
    captureCatalogScope: () => {
      const scope = capture(false, true)
      return () => current(scope)
    },
    catalog: (options = {}) => guarded((scope) => {
      assert(plain(options))
      const { intents = [], signal } = options
      assert(Array.isArray(intents))
      const entries = {}
      for (const supplied of intents) {
        const intent = checkIntent(supplied)
        assert(!Object.hasOwn(entries, intent.tool))
        Object.defineProperty(entries, intent.tool, { enumerable: true, value: {
          params: intent.params, catalog_digest: intent.catalogDigest, idempotency_key: intent.idempotencyKey,
          ...projectSolarRequestExtras(intent.tool, intent) } })
      }
      const body = intents.length ? JSON.stringify({ project_runs: entries }) : undefined
      assert(body === undefined || utf8(body) <= MAX_BODY)
      const b = binding
      const query = `drawing_id=${b.drawing_id}&project_id=${b.project_id}&input_version_id=${b.input_version_id}`
      return request(`/api/capabilities?${query}`, { method: intents.length ? 'POST' : 'GET', body,
        signal, scope, headers: intents.length ? proof() : {},
        validate: (data) => plain(data) && Array.isArray(data.families) })
    }, false, true),
    submitRun: (supplied, options = {}) => guarded((scope) => {
      assert(plain(options))
      const { signal } = options
      const intent = checkIntent(supplied)
      const built = createRunSubmissionRequest(intent.tool, intent.params, binding.input_version_id, {
        orgId: binding.organization_id, projectId: binding.project_id, catalogDigest: intent.catalogDigest,
        idempotencyKey: intent.idempotencyKey, checkoutCapability: capability,
        ...projectSolarIntentExtras(intent.tool, intent) })
      return request('/api/run', { scope, method: 'POST', body: JSON.stringify(built.body), headers: built.headers, signal,
        validate: (data) => typeof data?.job_id === 'string' && UUID.test(data.job_id) && data.status === 'submitted' })
    }),
    readJob: (jobId, options = {}) => guarded((scope) => {
      assert(plain(options))
      const { tool, signal } = options
      assert(typeof jobId === 'string' && UUID.test(jobId) && typeof tool === 'string' && TOOL.test(tool))
      return request(`/api/jobs/${jobId}`, { scope, signal, validate: (data, b) => validJob(data?.job || data, b, jobId, tool) })
    }),
    async observeJob(jobId, options = {}) {
      if (!plain(options)) return local('SIP_R7_REQUEST_INVALID')
      const { tool, onStatus, signal, intervalMs = 1000 } = options
      if (onStatus !== undefined && typeof onStatus !== 'function') return local('SIP_R7_REQUEST_INVALID')
      if (!Number.isFinite(intervalMs) || intervalMs < 250 || intervalMs > 30000) return local('SIP_R7_REQUEST_INVALID')
      const scope = capture()
      if (!scope.binding || disposed) return local('SIP_R7_BINDING_INVALID')
      while (current(scope)) {
        const outcome = await client.readJob(jobId, { tool, signal })
        if (!current(scope)) return local('SIP_R7_SCOPE_STALE')
        if (!outcome.ok) return outcome
        if (signal?.aborted) return local('SIP_R7_ABORTED')
        try { onStatus?.(outcome.data) } catch { /* observer isolation */ }
        if (!current(scope)) return local('SIP_R7_SCOPE_STALE')
        if (signal?.aborted) return local('SIP_R7_ABORTED')
        const status = (outcome.data.job || outcome.data).status
        if (['completed', 'complete', 'succeeded', 'failed', 'cancelled', 'canceled', 'error'].includes(status)) return outcome
        if (signal?.aborted) return local('SIP_R7_ABORTED')
        await new Promise((resolve) => {
          const controller = new AbortController()
          const operation = { ...scope, controller }
          operations.add(operation)
          const done = () => { clearTimeout(timer); signal?.removeEventListener('abort', done); operations.delete(operation); resolve() }
          const timer = setTimeout(done, intervalMs)
          controller.signal.addEventListener('abort', done, { once: true })
          signal?.addEventListener('abort', done, { once: true })
        })
        if (signal?.aborted) return local('SIP_R7_ABORTED')
      }
      return local('SIP_R7_SCOPE_STALE')
    },
    readSolar: (options = {}) => guarded((scope) => {
      assert(plain(options))
      const { tool, params = {}, catalogDigest, current: required = false, signal } = options
      assert(typeof tool === 'string' && TOOL.test(tool) && typeof catalogDigest === 'string' && DIGEST.test(catalogDigest) && typeof required === 'boolean')
      const body = JSON.stringify({ tool, params: parameters(params, binding), catalog_digest: catalogDigest, current: required })
      assert(utf8(body) <= MAX_BODY)
      return request(`/api/projects/${binding.project_id}/drawings/${binding.drawing_id}/versions/${binding.input_version_id}/solar-reads`,
        { scope, method: 'POST', body, signal, validate: (data, b) => {
          const result = data?.result
          return matches(result, b) && result.schema === 'leaf.solar-project-read.v1' && result.adapter === 'project-local-graph-read' &&
            result.tool === tool && result.tool_manifest_sha256 === catalogDigest && result.current_required === required && result.drawing_changed === false
        } })
    }),
    downloadArtifact: (ref, options = {}) => guarded((scope) => {
      assert(plain(options))
      const { current: required = false, ifNoneMatch, signal } = options
      assert(plain(ref) && Reflect.ownKeys(ref).length === REF_KEYS.length && REF_KEYS.every((key) => {
        const descriptor = Object.getOwnPropertyDescriptor(ref, key)
        return descriptor && Object.hasOwn(descriptor, 'value')
      }))
      assert(ref.schema === 'leaf.solar-project-artifact-ref.v1' && matches({ ...ref, input_version_id: ref.source_version_id }, binding) &&
        ['artifact_id', 'content_sha256', 'source_intake_sha256', 'graph_sha256'].every((key) => typeof ref[key] === 'string' && HEX.test(ref[key])) &&
        Number.isInteger(ref.byte_length) && ref.byte_length >= 1 && ref.byte_length <= MAX_ARTIFACT &&
        typeof ref.media_type === 'string' && ref.media_type.length > 0 && typeof ref.filename === 'string' && ref.filename.length > 0 &&
        typeof required === 'boolean')
      const path = `/api/projects/${binding.project_id}/drawings/${binding.drawing_id}/solar-artifacts/${ref.artifact_id}`
      assert(ref.download === path && (ifNoneMatch === undefined || (typeof ifNoneMatch === 'string' && /^"[0-9a-f]{64}"$/.test(ifNoneMatch))))
      return request(`${path}?current=${required}`, { scope, signal, headers: ifNoneMatch === undefined ? {} : { 'If-None-Match': ifNoneMatch },
        consume: async (response, interrupted) => {
          if (response.status === 304) return { notModified: true, bytes: null }
          assert(response.status === 200)
          const length = response.headers?.get('Content-Length')
          assert(length === null || length === undefined || (Number.isFinite(Number(length)) && Number(length) <= MAX_ARTIFACT))
          const chunks = []
          let total = 0
          if (response.body?.getReader) {
            const reader = response.body.getReader()
            try {
              while (true) {
                const { done, value } = await reader.read()
                if (interrupted()) { await reader.cancel(); throw new TypeError('Stale artifact') }
                if (done) break
                assert(ArrayBuffer.isView(value))
                const intrinsic = Object.getPrototypeOf(Uint8Array.prototype)
                const size = Object.getOwnPropertyDescriptor(intrinsic, 'byteLength').get.call(value)
                const offset = Object.getOwnPropertyDescriptor(intrinsic, 'byteOffset').get.call(value)
                const buffer = Object.getOwnPropertyDescriptor(intrinsic, 'buffer').get.call(value)
                total += size
                assert(total <= MAX_ARTIFACT)
                chunks.push(new Uint8Array(new Uint8Array(buffer, offset, size)))
              }
            } catch (failure) { await reader.cancel().catch(() => {}); throw failure }
            finally { reader.releaseLock() }
          } else {
            const buffer = await response.arrayBuffer()
            assert(buffer.byteLength <= MAX_ARTIFACT)
            chunks.push(new Uint8Array(buffer)); total = buffer.byteLength
          }
          const bytes = new Uint8Array(total)
          let offset = 0
          for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
          const headers = {}
          for (const key of ['Content-Type', 'Content-Length', 'ETag', 'X-Leaf-Artifact-Id', 'Content-Disposition', 'X-Content-Type-Options']) {
            const value = response.headers?.get(key)
            if (typeof value === 'string' && !CONTAINS_CAPABILITY.test(value)) headers[key] = value
          }
          return { notModified: false, bytes, headers: Object.freeze(headers) }
        } })
    }),
    dispose() {
      disposed = true
      bindingGeneration += 1
      checkoutGeneration += 1
      abort(() => true)
      binding = null; authorityKey = null; checkout = null
      mutation = null; pending = false; error = null
      rotate(null, null)
      listeners.clear()
    },
  }
  publish()
  return Object.freeze(client)
}

function validJob(data, binding, jobId, tool) {
  return typeof jobId === 'string' && UUID.test(jobId) && data?.job_id === jobId &&
    data.org_id === binding.organization_id && data.project_id === binding.project_id &&
    data.input_version_id === binding.input_version_id && data.tool_name === tool &&
    data.execution_context?.drawing_id === binding.drawing_id &&
    data.execution_context?.schema === 'leaf.solar-project-graph-job.v1'
}
