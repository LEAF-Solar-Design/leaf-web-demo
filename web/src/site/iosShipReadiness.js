/** Browser-safe projection for the migrated Wave D one-shot ship lane. */
import { authHeaders, config, noteUnauthorized } from '../api.js'

export const IOS_SHIP_READINESS_KIND = 'leaf.ios-ship-readiness.v1'
export const IOS_TESTFLIGHT_RECEIPT_KIND = 'leaf.ios-testflight-receipt.v1'
export const IOS_SHIP_SETUP_ACTION = 'mount-apple-ship-dispatch'

export const IOS_SOURCE_FIELDS = Object.freeze([
  'source_revision', 'source_sha256', 'bundle_identifier', 'marketing_version', 'build_number',
])

const SOURCE_GRAMMARS = Object.freeze({
  source_revision: /^[0-9a-f]{40}$/, source_sha256: /^[0-9a-f]{64}$/,
  bundle_identifier: /^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/,
  marketing_version: /^[0-9]{1,18}(\.[0-9]{1,18}){1,2}$/, build_number: /^[0-9]{1,18}$/,
  catalog_key: /^[a-z0-9][a-z0-9_-]{0,63}$/, repository: /^https:\/\/\S+$/,
})
const fullMatch = (pattern, value) => pattern.exec(value)?.[0] === value

function validateSourceApproval(entry, invalid) {
  if (!entry || typeof entry !== 'object' || Array.isArray(entry)) invalid('approval')
  const approval = {}
  for (const field of ['approval_id', 'revision', 'source_revision']) {
    const value = entry[field]
    if (typeof value !== 'string' || !value.length || value.length > 512 || /[\x00-\x1f\x7f-\x9f]/.test(value)) invalid(field)
    if (field === 'source_revision' && !fullMatch(SOURCE_GRAMMARS.source_revision, value)) invalid(field)
    approval[field] = value
  }
  if (entry.consumed_at != null && (typeof entry.consumed_at !== 'string' || entry.consumed_at.length > 64)) invalid('consumed_at')
  return Object.freeze({ ...approval, consumed_at: entry.consumed_at ?? null })
}

export function validateIosShipSources(data) {
  const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
  const invalid = (field) => { throw new Error(`source catalog field invalid: ${field}`) }
  const string = (value, field, limit = 512) => {
    if (typeof value !== 'string' || !value.length || value.length > limit) invalid(field)
    return value
  }
  if (!object(data) || data.ok !== true) invalid('ok')
  const secret = hasSecretShapedField(data)
  if (secret) invalid(typeof secret === 'string' ? secret : 'secret-shaped field')
  const list = (field, project) => {
    if (!Array.isArray(data[field]) || data[field].length > 50) invalid(field)
    return Object.freeze(data[field].map((entry) => {
      if (!object(entry)) invalid(field)
      return Object.freeze(project(entry))
    }))
  }
  const sources = list('sources', (entry) => {
    const source = Object.fromEntries(IOS_SOURCE_FIELDS.map((field) => [field, string(entry[field], field)]))
    for (const field of IOS_SOURCE_FIELDS) {
      if (!fullMatch(SOURCE_GRAMMARS[field], source[field])) invalid(field)
    }
    if (source.bundle_identifier.length > 255) invalid('bundle_identifier')
    for (const field of ['catalog_key', 'repository', 'imported_at']) {
      if (entry[field] === undefined) continue
      const value = entry[field]
      if (typeof value !== 'string' || value.length > (field === 'imported_at' ? 64 : 512)) invalid(field)
      if (SOURCE_GRAMMARS[field] && !fullMatch(SOURCE_GRAMMARS[field], value)) invalid(field)
      source[field] = value
    }
    return source
  })
  const approvals = list('approvals', (entry) => validateSourceApproval(entry, invalid))
  let sync = null
  if (data.sync !== undefined) {
    if (!object(data.sync)) invalid('sync')
    if (typeof data.sync.status !== 'string' || !/^[a-z_]{1,32}$/.test(data.sync.status)) invalid('sync.status')
    if (!Number.isInteger(data.sync.registered) || data.sync.registered < 0) invalid('sync.registered')
    sync = { status: data.sync.status, registered: data.sync.registered }
    for (const field of ['conflicts', 'unpinned', 'refused']) {
      if (data.sync[field] !== undefined && !Array.isArray(data.sync[field])) invalid(`sync.${field}`)
      sync[field] = data.sync[field]?.length || 0
    }
    Object.freeze(sync)
  }
  return Object.freeze({ sources, approvals, sync, canApprove: data.can_approve === true })
}

export function iosSourceApprovalState(source, approvals, revision) {
  if (!revision) return 'unapproved'
  const matches = approvals.filter((approval) => approval.revision === revision && approval.source_revision === source.source_revision)
  if (matches.some((approval) => approval.consumed_at != null)) return 'consumed'
  return matches.length ? 'approved' : 'unapproved'
}

export async function fetchIosShipSources({ projectId, fetchImpl = globalThis.fetch }) {
  const path = `/api/projects/${encodeURIComponent(projectId)}/ios/sources`
  const res = await iosFetch(path, { headers: { accept: 'application/json' } }, fetchImpl)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const error = new Error(data?.error?.message || `sources unavailable (${res.status})`)
    error.status = res.status
    error.envelope = data?.error || null
    throw error
  }
  return validateIosShipSources(data)
}

export async function requestIosShipApproval({ projectId, revision, source, fetchImpl = globalThis.fetch }) {
  const body = { revision }
  for (const field of IOS_SOURCE_FIELDS) body[field] = source?.[field]
  for (const [field, value] of Object.entries(body)) {
    if (typeof value !== 'string' || !value.length || value.length > 512) throw new Error(`approval request field invalid: ${field}`)
  }
  const res = await iosFetch(`/api/projects/${encodeURIComponent(projectId)}/ios/approvals`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  }, fetchImpl)
  const data = await res.json().catch(() => ({}))
  if (!res.ok || data.ok !== true || hasSecretShapedField(data)) {
    const error = new Error(data?.error?.message || `approval unavailable (${res.status})`)
    error.status = res.status
    error.code = data?.error?.code
    error.envelope = data?.error || null
    throw error
  }
  return validateSourceApproval(data.approval, (field) => { throw new Error(`approval response field invalid: ${field}`) })
}

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
    return Object.freeze({ ...emptyIosShipReadiness(
      typeof record.reason === 'string' ? record.reason : 'unhealthy',
      typeof record.setup_action === 'string' ? record.setup_action : null,
      typeof record.project_id === 'string' ? record.project_id : expected.projectId,
    ), grantStatus: typeof record.grant_status === 'string' ? record.grant_status : null })
  }
  const approvedLaunch = validateApprovedLaunch(record.approved_launch)
  if (!approvedLaunch || record.dispatch_available !== true || record.grant_status !== 'healthy') {
    return Object.freeze({
      ...emptyIosShipReadiness('invalid_record', null, expected.projectId),
      grantStatus: typeof record.grant_status === 'string' ? record.grant_status : null,
    })
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
    error.status = res.status
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
