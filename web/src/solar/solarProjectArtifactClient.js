import { createSolarDrawingBinding, sameSolarDrawingBinding } from './solarDrawingBinding.js'

export const SOLAR_PROJECT_ARTIFACT_MAX_BYTES = 16_777_216
export const SOLAR_PROJECT_ARTIFACT_TIMEOUT_MS = 60_000

const HEX = new RegExp('^[0-9a-f]{64}$')
const NAME = new RegExp('^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\\.(csv|json|xlsx|kml|xml|pdf)$')
const MEDIA = Object.freeze({
  'text/csv': 'csv', 'application/json': 'json',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
  'application/vnd.google-earth.kml+xml': 'kml', 'application/xml': 'xml', 'application/pdf': 'pdf',
})
const KEYS = ['organization_id', 'project_id', 'drawing_id', 'source_version_id',
  'source_intake_sha256', 'graph_sha256', 'artifact_id', 'media_type', 'filename',
  'byte_length', 'content_sha256', 'schema', 'download']
const local = (reason, status = null) => ({ ok: false, status, reason, envelope: null })
const hash = (value) => typeof value === 'string' && HEX.test(value)

function copyRef(ref, suppliedBinding, hashes) {
  const binding = createSolarDrawingBinding(suppliedBinding)
  if (!ref || typeof ref !== 'object' ||
      ![Object.prototype, null].includes(Object.getPrototypeOf(ref)) || Reflect.ownKeys(ref).length !== KEYS.length) return null
  const copy = {}
  for (const key of KEYS) {
    const descriptor = Object.getOwnPropertyDescriptor(ref, key)
    if (!descriptor || !Object.hasOwn(descriptor, 'value')) return null
    copy[key] = descriptor.value
  }
  if (copy.schema !== 'leaf.solar-project-artifact-ref.v1' ||
      copy.organization_id !== binding.organization_id || copy.project_id !== binding.project_id ||
      copy.drawing_id !== binding.drawing_id || copy.source_version_id !== binding.input_version_id ||
      !hash(hashes.inputIntakeSha256) || !hash(hashes.graphSha256) ||
      copy.source_intake_sha256 !== hashes.inputIntakeSha256 || copy.graph_sha256 !== hashes.graphSha256 ||
      !hash(copy.artifact_id) || !hash(copy.content_sha256) ||
      !Number.isInteger(copy.byte_length) || copy.byte_length < 1 || copy.byte_length > SOLAR_PROJECT_ARTIFACT_MAX_BYTES ||
      typeof copy.media_type !== 'string' || !Object.hasOwn(MEDIA, copy.media_type) ||
      typeof copy.filename !== 'string' || !NAME.test(copy.filename) || copy.filename.includes('..') ||
      !copy.filename.endsWith('.' + MEDIA[copy.media_type]) ||
      copy.download !== `/api/projects/${binding.project_id}/drawings/${binding.drawing_id}/solar-artifacts/${copy.artifact_id}`) return null
  return Object.freeze(copy)
}

export function validateSolarProjectArtifactRef(ref, binding, hashes) {
  try { return !!copyRef(ref, binding, hashes) } catch { return false }
}

async function defaultSha256(bytes) {
  const value = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(value), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

// Intrinsic getters accept cross-realm views and cannot be fooled by own size properties.
function privateBytes(value) {
  const intrinsic = Object.getPrototypeOf(Uint8Array.prototype)
  if (Object.getOwnPropertyDescriptor(intrinsic, Symbol.toStringTag).get.call(value) !== 'Uint8Array') return null
  const size = Object.getOwnPropertyDescriptor(intrinsic, 'byteLength').get.call(value)
  if (size < 1 || size > SOLAR_PROJECT_ARTIFACT_MAX_BYTES) return null
  const offset = Object.getOwnPropertyDescriptor(intrinsic, 'byteOffset').get.call(value)
  const buffer = Object.getOwnPropertyDescriptor(intrinsic, 'buffer').get.call(value)
  return new Uint8Array(new Uint8Array(buffer, offset, size))
}

export function createSolarProjectArtifactClient({ projectClient, digest = defaultSha256,
  timeoutMs = SOLAR_PROJECT_ARTIFACT_TIMEOUT_MS } = {}) {
  if (!projectClient || !['downloadArtifact', 'getSnapshot', 'captureCatalogScope', 'subscribe'].every((key) =>
    typeof projectClient[key] === 'function') || typeof digest !== 'function' ||
    !Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new TypeError('Invalid project artifact client')

  async function downloadArtifact(options) {
    let controller, timer, unsubscribe, signal, callerAbort, cancelPending, settlementCheck
    let timedOut = false, scopeLost = false
    try {
      const { binding: suppliedBinding, authorityKey, ref, inputIntakeSha256, graphSha256,
        current = true, isCurrent } = options
      let binding
      try { binding = createSolarDrawingBinding(suppliedBinding) }
      catch { return local('SIP_R12_BINDING_INVALID') }
      if (typeof authorityKey !== 'string' || !authorityKey.trim() || typeof current !== 'boolean' ||
          typeof isCurrent !== 'function') return local('SIP_R12_REQUEST_INVALID')
      signal = options.signal
      if (signal !== undefined && (!signal || typeof signal.aborted !== 'boolean' ||
          typeof signal.addEventListener !== 'function' || typeof signal.removeEventListener !== 'function')) return local('SIP_R12_REQUEST_INVALID')
      let copiedRef
      try { copiedRef = copyRef(ref, binding, { inputIntakeSha256, graphSha256 }) }
      catch { copiedRef = null }
      if (!copiedRef) return local('SIP_R12_ARTIFACT_REF_INVALID')
      const captured = projectClient.captureCatalogScope()
      if (typeof captured !== 'function') return local('SIP_R12_SCOPE_STALE')
      const scopeCurrent = () => {
        try {
          const snapshot = projectClient.getSnapshot()
          return !scopeLost && isCurrent() === true && captured() === true &&
            sameSolarDrawingBinding(snapshot.binding, binding) && snapshot.authorityKey === authorityKey
        } catch { return false }
      }
      controller = new AbortController()
      const cancelled = new Promise((resolve) => { cancelPending = () => resolve(null) })
      callerAbort = () => { controller.abort(); cancelPending() }
      const interrupted = () => !scopeCurrent() ? local('SIP_R12_SCOPE_STALE') :
        timedOut ? local('SIP_R12_TIMEOUT') : controller.signal.aborted ? local('SIP_R12_ABORTED') : null
      unsubscribe = projectClient.subscribe(() => {
        if (!scopeCurrent()) { scopeLost = true; callerAbort() }
      })
      signal?.addEventListener('abort', callerAbort, { once: true })
      if (signal?.aborted) callerAbort()
      const deadline = Date.now() + timeoutMs
      timer = setTimeout(() => { timedOut = true; callerAbort() }, timeoutMs)
      const check = () => {
        if (Date.now() >= deadline) timedOut = true
        return interrupted()
      }
      settlementCheck = check
      if (check()) return check()
      const response = await Promise.race([
        Promise.resolve().then(() => check() || projectClient.downloadArtifact(copiedRef, { current, signal: controller.signal })),
        cancelled,
      ])
      if (check()) return check()
      if (response?.ok === false) return response
      const status = response?.status ?? null
      if (response?.ok !== true || status !== 200 || response.data?.notModified !== false) return local('SIP_R12_RESPONSE_INVALID', status)
      let bytes
      try { bytes = privateBytes(response.data.bytes) } catch { bytes = null }
      const headers = response.data.headers
      if (!bytes || !headers || typeof headers['ETag'] !== 'string' || typeof headers['X-Leaf-Artifact-Id'] !== 'string' ||
          typeof headers['Content-Type'] !== 'string') return local('SIP_R12_RESPONSE_INVALID', status)
      const contentType = headers['Content-Type'].split(';')
      if (bytes.byteLength !== copiedRef.byte_length || headers['ETag'] !== '"' + copiedRef.content_sha256 + '"' ||
          headers['X-Leaf-Artifact-Id'] !== copiedRef.artifact_id || contentType[0].trim() !== copiedRef.media_type ||
          contentType.length > 2 || (contentType.length === 2 && !/^\s*charset=utf-8\s*$/i.test(contentType[1])) ||
          (headers['Content-Length'] !== undefined && (!/^[0-9]+$/.test(headers['Content-Length']) || Number(headers['Content-Length']) !== bytes.byteLength)) ||
          (headers['Content-Disposition'] !== undefined && headers['Content-Disposition'] !== `attachment; filename="${copiedRef.filename}"`) ||
          (headers['X-Content-Type-Options'] !== undefined && headers['X-Content-Type-Options'] !== 'nosniff')) return local('SIP_R12_ARTIFACT_MISMATCH', status)
      let contentSha256
      try {
        // The injected digest gets its own copy; it cannot mutate the saved outcome bytes.
        contentSha256 = await Promise.race([Promise.resolve().then(() => digest(new Uint8Array(bytes))), cancelled])
      } catch {
        return check() || local('SIP_R12_DIGEST_UNAVAILABLE', status)
      }
      if (check()) return check()
      if (!hash(contentSha256)) return local('SIP_R12_DIGEST_UNAVAILABLE', status)
      if (contentSha256 !== copiedRef.content_sha256) return local('SIP_R12_ARTIFACT_MISMATCH', status)
      return Object.freeze({ ok: true, status: 200, data: Object.freeze({ binding,
        artifactId: copiedRef.artifact_id, filename: copiedRef.filename, mediaType: copiedRef.media_type,
        byteLength: bytes.byteLength, contentSha256, bytes }) })
    } catch {
      return settlementCheck?.() || local(scopeLost ? 'SIP_R12_SCOPE_STALE' : timedOut ? 'SIP_R12_TIMEOUT' :
        controller?.signal.aborted ? 'SIP_R12_ABORTED' : 'SIP_R12_REQUEST_INVALID')
    } finally {
      clearTimeout(timer)
      try { unsubscribe?.() } catch { /* observer isolation */ }
      try { signal?.removeEventListener('abort', callerAbort) } catch { /* malformed caller */ }
    }
  }
  return Object.freeze({ downloadArtifact })
}
