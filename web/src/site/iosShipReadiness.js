/** Browser-safe projection for the migrated Wave D one-shot ship lane. */
import { authHeaders, config, noteUnauthorized } from '../api.js'

export const IOS_SHIP_READINESS_KIND = 'leaf.ios-ship-readiness.v1'
export const IOS_TESTFLIGHT_RECEIPT_KIND = 'leaf.ios-testflight-receipt.v1'
export const IOS_SHIP_SETUP_ACTION = 'mount-apple-ship-dispatch'

const SHA256 = /^[0-9a-f]{64}$/
const SECRET_KEY_RE = /(password|passwd|two.?factor|2fa|otp|p8|\.p8|private[_ -]?key|certificate|provisioning|profile|credential|secret|keychain|authkey|token|session|cookie|api[_ -]?key|signing[_ -]?(key|cert))/i
const APPROVED_FIELDS = [
  'approval_id', 'revision', 'source_revision', 'source_sha256',
  'bundle_identifier', 'marketing_version', 'build_number',
]

export function hasSecretShapedField(value, path = '$') {
  if (value == null) return null
  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i += 1) {
      const hit = hasSecretShapedField(value[i], `${path}[${i}]`)
      if (hit) return hit
    }
    return null
  }
  if (typeof value === 'object') {
    for (const [key, child] of Object.entries(value)) {
      if (SECRET_KEY_RE.test(key)) return `${path}.${key}`
      const hit = hasSecretShapedField(child, `${path}.${key}`)
      if (hit) return hit
    }
    return null
  }
  if (typeof value === 'string' && /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/i.test(value)) return path
  return null
}

export function emptyIosShipReadiness(reason = 'unavailable', setupAction = null, projectId = null) {
  return Object.freeze({
    kind: IOS_SHIP_READINESS_KIND,
    projectId,
    launchable: false,
    healthy: false,
    reportedAt: null,
    grantStatus: null,
    dispatchAvailable: false,
    approvedLaunch: null,
    reason,
    setupAction,
  })
}

function validateApprovedLaunch(value) {
  if (!value || typeof value !== 'object') return null
  if (Object.keys(value).sort().join('|') !== [...APPROVED_FIELDS].sort().join('|')) return null
  for (const field of APPROVED_FIELDS) {
    if (typeof value[field] !== 'string' || !value[field]) return null
  }
  if (!SHA256.test(value.source_sha256) || hasSecretShapedField(value)) return null
  return Object.freeze({ ...value })
}

export function validateIosShipReadiness(record, expected = {}) {
  if (!record || typeof record !== 'object' || hasSecretShapedField(record)) {
    return emptyIosShipReadiness('invalid_record', null, expected.projectId)
  }
  if (record.record_kind !== IOS_SHIP_READINESS_KIND) {
    return emptyIosShipReadiness('invalid_record', null, expected.projectId)
  }
  if (record.launchable !== true || record.healthy !== true) {
    return emptyIosShipReadiness(
      typeof record.reason === 'string' ? record.reason : 'unhealthy',
      typeof record.setup_action === 'string' ? record.setup_action : null,
      typeof record.project_id === 'string' ? record.project_id : expected.projectId,
    )
  }
  const approvedLaunch = validateApprovedLaunch(record.approved_launch)
  if (!approvedLaunch || record.dispatch_available !== true || record.grant_status !== 'healthy') {
    return emptyIosShipReadiness('invalid_record', null, expected.projectId)
  }
  if (typeof record.project_id !== 'string' || record.project_id !== expected.projectId) {
    return emptyIosShipReadiness('project_mismatch', null, expected.projectId)
  }
  if (expected.revision && approvedLaunch.revision !== expected.revision) {
    return emptyIosShipReadiness('revision_mismatch', null, expected.projectId)
  }
  return Object.freeze({
    kind: IOS_SHIP_READINESS_KIND,
    projectId: record.project_id,
    launchable: true,
    healthy: true,
    reportedAt: typeof record.reported_at === 'string' ? record.reported_at : null,
    grantStatus: record.grant_status,
    dispatchAvailable: true,
    approvedLaunch,
    reason: null,
    setupAction: null,
  })
}

export function iosShipLaunchAffordance(readiness, context = {}) {
  const { projectId = null, revision = null, sessionActive = false } = context || {}
  return readiness?.launchable === true
    && readiness.projectId === projectId
    && readiness.approvedLaunch?.revision === revision
    && sessionActive === true
}

export function makeIosShipLaunchKey(projectId, approvedLaunch) {
  const approval = typeof approvedLaunch === 'string' ? approvedLaunch : approvedLaunch?.approval_id
  return `ios-ship:${projectId}:${approval}`
}

async function iosFetch(path, init, fetchImpl) {
  const headers = {
    'X-Tenant-Id': config.tenant,
    ...(init?.headers || {}),
    ...authHeaders(),
  }
  return noteUnauthorized(
    await fetchImpl(`${config.apiBase}${path}`, { ...(init || {}), headers }),
    path,
    headers.Authorization,
  )
}

export async function fetchIosShipReadiness({ projectId, revision, fetchImpl = globalThis.fetch } = {}) {
  try {
    const path = `/api/ios-ship/readiness?project_id=${encodeURIComponent(projectId)}&revision=${encodeURIComponent(revision)}`
    const res = await iosFetch(path, { headers: { accept: 'application/json' } }, fetchImpl)
    if (!res.ok) return emptyIosShipReadiness(`http_${res.status}`, null, projectId)
    const data = await res.json()
    return validateIosShipReadiness(data?.readiness, { projectId, revision })
  } catch {
    return emptyIosShipReadiness('unreachable', null, projectId)
  }
}

export async function requestIosShipLaunch({
  projectId, approvedLaunch, idempotencyKey, fetchImpl = globalThis.fetch,
}) {
  const approved = validateApprovedLaunch(approvedLaunch)
  if (!approved) throw new Error('The approved iOS launch record is invalid.')
  const res = await iosFetch('/api/ios-ship/launch', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify({ project_id: projectId, ...approved }),
  }, fetchImpl)
  const data = await res.json().catch(() => ({}))
  if (!res.ok || data.ok !== true || hasSecretShapedField(data)) {
    const error = new Error(data?.error?.message || `iOS launch failed (${res.status})`)
    error.envelope = data?.error || null
    throw error
  }
  return data
}

export async function getIosShipExecution({ projectId, executionId, fetchImpl = globalThis.fetch }) {
  const path = `/api/ios-ship/executions/${encodeURIComponent(executionId)}?project_id=${encodeURIComponent(projectId)}`
  const res = await iosFetch(path, { headers: { accept: 'application/json' } }, fetchImpl)
  const data = await res.json().catch(() => ({}))
  if (!res.ok || data.ok !== true || hasSecretShapedField(data)) {
    const error = new Error(data?.error?.message || `execution unavailable (${res.status})`)
    error.envelope = data?.error || null
    throw error
  }
  return data
}

export async function getIosShipReceipt({ projectId, receiptId, fetchImpl = globalThis.fetch }) {
  const path = `/api/ios-ship/receipts/${encodeURIComponent(receiptId)}?project_id=${encodeURIComponent(projectId)}`
  const res = await iosFetch(path, { headers: { accept: 'application/json' } }, fetchImpl)
  const data = await res.json().catch(() => ({}))
  if (!res.ok || data.ok !== true || hasSecretShapedField(data)
      || data.receipt?.kind !== IOS_TESTFLIGHT_RECEIPT_KIND) {
    const error = new Error(data?.error?.message || `receipt unavailable (${res.status})`)
    error.envelope = data?.error || null
    throw error
  }
  return data
}
