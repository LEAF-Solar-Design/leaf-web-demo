export const INFLIGHT_AUTHOR_KEY = 'leaf.inflightAuthor.v1'
export const AUTHOR_POINTER_TTL_MS = 24 * 60 * 60 * 1000

const FAILURE_MESSAGE = 'Authoring failed. Your request is saved for recovery.'
const REASON_CODE = /^[a-z][a-z0-9_]{0,63}$/

export function boundedAuthorFailure(cause) {
  const reason = cause?.body?.error?.reason_code ?? cause?.body?.reason_code
    ?? cause?.body?.error?.error_code ?? cause?.code
  return {
    status: typeof cause?.status === 'number' && Number.isFinite(cause.status) ? cause.status : null,
    reason_code: typeof reason === 'string' && REASON_CODE.test(reason) ? reason : null,
    message: FAILURE_MESSAGE,
  }
}

function validFailure(failure) {
  return failure && typeof failure === 'object' && !Array.isArray(failure)
    && Object.keys(failure).sort().join(',') === 'message,reason_code,status'
    && (failure.status === null || (typeof failure.status === 'number' && Number.isFinite(failure.status)))
    && (failure.reason_code === null || (typeof failure.reason_code === 'string' && REASON_CODE.test(failure.reason_code)))
    && failure.message === FAILURE_MESSAGE
}

function validPriorFailure(prior) {
  return prior && typeof prior === 'object' && !Array.isArray(prior)
    && Object.keys(prior).sort().join(',') === 'change_set_id,failed_at,idempotency_key,poll_url'
    && typeof prior.idempotency_key === 'string' && prior.idempotency_key.length > 0
    && typeof prior.change_set_id === 'string' && prior.change_set_id.length > 0
    && typeof prior.poll_url === 'string' && prior.poll_url.length > 0
    && typeof prior.failed_at === 'number' && Number.isFinite(prior.failed_at)
}

function browserStorage() {
  try { return window.localStorage } catch { return null }
}

function jwtSubject(storage) {
  try {
    const token = storage?.getItem('leaf.jwt') || ''
    const payload = token.split('.')[1]
    if (!payload) return null
    const raw = payload.replace(/-/g, '+').replace(/_/g, '/')
    const normalized = raw.padEnd(Math.ceil(raw.length / 4) * 4, '=')
    return JSON.parse(atob(normalized)).sub || null
  } catch {
    return null
  }
}

export function authorAccountScope(tenant, storage = browserStorage()) {
  let org = null
  try { org = storage?.getItem('leaf.org_id') || null } catch { /* absent */ }
  const subject = jwtSubject(storage)
  return `tenant:${tenant || 'default'}|org:${org || 'none'}|subject:${subject || 'anonymous'}`
}

export function readInflightAuthor(storage = browserStorage()) {
  try {
    const pointer = JSON.parse(storage?.getItem(INFLIGHT_AUTHOR_KEY) || 'null')
    if (!pointer || typeof pointer !== 'object') return null
    if (typeof pointer.idempotency_key !== 'string' || !pointer.idempotency_key) return null
    if (typeof pointer.description !== 'string' || !pointer.description.trim()) return null
    if (typeof pointer.account_scope !== 'string' || !pointer.account_scope) return null
    if (pointer.target_tool_name != null && typeof pointer.target_tool_name !== 'string') return null
    if (!Number.isFinite(Number(pointer.created_at)) || !Number.isFinite(Number(pointer.expires_at))) return null
    if (pointer.poll_url != null && typeof pointer.poll_url !== 'string') return null
    if (pointer.terminal_failed !== undefined && typeof pointer.terminal_failed !== 'boolean') return null
    if (pointer.failed_at !== undefined && (typeof pointer.failed_at !== 'number' || !Number.isFinite(pointer.failed_at))) return null
    if (pointer.failure !== undefined && !validFailure(pointer.failure)) return null
    if (pointer.terminal_failed && (!validFailure(pointer.failure) || !Number.isFinite(pointer.failed_at))) return null
    if (pointer.prior_failure !== undefined && !validPriorFailure(pointer.prior_failure)) return null
    return pointer
  } catch {
    return null
  }
}

export function authorPointerValid(pointer, accountScope, now = Date.now()) {
  if (!pointer) return false
  return pointer.account_scope === accountScope && Number(pointer.expires_at) > now
}

export function saveInflightAuthor(pointer, storage = browserStorage()) {
  try { storage?.setItem(INFLIGHT_AUTHOR_KEY, JSON.stringify(pointer)) } catch { /* best effort */ }
  return pointer
}

export function clearInflightAuthor(idempotencyKey = null, storage = browserStorage()) {
  try {
    if (idempotencyKey) {
      const current = readInflightAuthor(storage)
      if (current?.idempotency_key !== idempotencyKey) return false
    }
    storage?.removeItem(INFLIGHT_AUTHOR_KEY)
    return true
  } catch {
    return false
  }
}
