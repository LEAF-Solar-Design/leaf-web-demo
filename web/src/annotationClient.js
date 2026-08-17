import { authHeaders, config, noteUnauthorized } from './api.js'

const API_BASE = config.apiBase
const TENANT = config.tenant

const STATES = new Set(['pending', 'accepted', 'rejected', 'expired', 'stale'])
const KINDS = new Set(['apply', 'undo'])
const HEX40 = /^[0-9a-f]{40}$/
const HEX64 = /^[0-9a-f]{64}$/

function headers(json = false) {
  return {
    ...(json ? { 'Content-Type': 'application/json' } : {}),
    'X-Tenant-Id': TENANT,
    ...authHeaders(),
  }
}

function safeError(status = 0) {
  const error = new Error('Annotation status could not be updated. Nothing changed.')
  error.status = status
  return error
}

function nullable(value, check) {
  return value === null || check(value)
}

export function projectAnnotation(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw safeError()
  const isInt = (value) => Number.isInteger(value) && value >= 0
  const isString = (value) => typeof value === 'string' && value.length > 0
  if (!isString(body.decision_copy)
      || !isString(body.batch_id)
      || !isInt(body.revision)
      || !STATES.has(body.state)
      || !KINDS.has(body.kind)
      || !HEX64.test(body.payload_digest)
      || !Number.isInteger(body.payload_count) || body.payload_count < 1
      || !isInt(body.base_version)
      || !HEX40.test(body.base_commit) || !HEX40.test(body.base_tree)
      || !HEX40.test(body.preview_commit) || !HEX40.test(body.preview_tree)
      || !nullable(body.retry_of_batch_id, isString)
      || !nullable(body.reverses_batch_id, isString)
      || !nullable(body.reverses_commit, (value) => HEX40.test(value))
      || !nullable(body.reverses_tree, (value) => HEX40.test(value))
      || !nullable(body.applied_version, isInt)
      || !isInt(body.target_version)
      || !HEX40.test(body.target_commit) || !HEX40.test(body.target_tree)) {
    throw safeError()
  }
  return Object.freeze({
    decisionCopy: body.decision_copy,
    batchId: body.batch_id,
    revision: body.revision,
    state: body.state,
    kind: body.kind,
    payloadDigest: body.payload_digest,
    payloadCount: body.payload_count,
    baseVersion: body.base_version,
    baseCommit: body.base_commit,
    baseTree: body.base_tree,
    previewCommit: body.preview_commit,
    previewTree: body.preview_tree,
    retryOfBatchId: body.retry_of_batch_id,
    reversesBatchId: body.reverses_batch_id,
    reversesCommit: body.reverses_commit,
    reversesTree: body.reverses_tree,
    appliedVersion: body.applied_version,
    targetVersion: body.target_version,
    targetCommit: body.target_commit,
    targetTree: body.target_tree,
  })
}

export async function fetchCurrentAnnotation(sessionId) {
  const path = `/api/overlay/annotations/current?session_id=${encodeURIComponent(sessionId)}`
  const requestHeaders = headers()
  let res
  try {
    res = await fetch(`${API_BASE}${path}`, { method: 'GET', headers: requestHeaders })
  } catch {
    throw safeError()
  }
  noteUnauthorized(res, '/api/overlay/annotations/current', requestHeaders.Authorization)
  if (res.status === 204 || res.status === 404) return null
  const body = await res.json().catch(() => null)
  if (!res.ok) throw safeError(res.status)
  return projectAnnotation(body)
}

async function mutate(batchId, action, keyName, key) {
  const path = `/api/overlay/annotations/${encodeURIComponent(batchId)}/${action}`
  const requestHeaders = headers(true)
  let res
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: requestHeaders,
      body: JSON.stringify({ [keyName]: key }),
    })
  } catch {
    throw safeError()
  }
  const body = await res.json().catch(() => null)
  noteUnauthorized(res, path, requestHeaders.Authorization)
  if (!res.ok) throw safeError(res.status)
  return projectAnnotation(body)
}

export const acceptAnnotation = (batchId, decisionKey) => (
  mutate(batchId, 'accept', 'decision_key', decisionKey)
)
export const rejectAnnotation = (batchId, decisionKey) => (
  mutate(batchId, 'reject', 'decision_key', decisionKey)
)
export const retryAnnotation = (batchId, requestKey) => (
  mutate(batchId, 'retry', 'request_key', requestKey)
)
export const undoAnnotation = (batchId, requestKey) => (
  mutate(batchId, 'undo', 'request_key', requestKey)
)
