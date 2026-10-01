import { config, authHeaders, noteUnauthorized } from './api.js'

const TIMEOUT_MS = 10_000

// The same tenant/auth/401 seam as converse.js; transport failures are values
// here because this optional inbox must never break the conversation.
async function request(path, method = 'GET', options = {}) {
  let timer
  let timedOut = false
  try {
    const timeoutMs = options?.timeoutMs ?? TIMEOUT_MS
    const controller = new AbortController()
    const headers = { 'X-Tenant-Id': config.tenant, ...authHeaders() }
    if (method === 'POST') headers['Content-Type'] = 'application/json'
    const timeout = new Promise((resolve) => {
      timer = setTimeout(() => {
        timedOut = true
        controller.abort()
        resolve({ kind: 'unavailable', reason: 'timeout' })
      }, Math.max(1, Number(timeoutMs) || TIMEOUT_MS))
    })
    const response = (async () => {
      try {
        const res = await fetch(`${config.apiBase}${path}`, {
          method, headers, signal: controller.signal,
          ...(method === 'POST' ? { body: '{}' } : {}),
        })
        noteUnauthorized(res, path, headers.Authorization)
        if (res.status === 403) return { kind: 'forbidden' }
        if (res.status === 503) return { kind: 'unavailable', reason: 'store' }
        if (!res.ok) return { kind: 'error', httpStatus: res.status }
        if (res.status === 204) return { kind: 'ok', data: {} }
        const data = await res.json().catch(() => null)
        return data && typeof data === 'object' && !Array.isArray(data)
          ? { kind: 'ok', data }
          : { kind: 'error', reason: 'response' }
      } catch {
        return { kind: 'unavailable', reason: timedOut ? 'timeout' : 'network' }
      }
    })()
    // Also bounds a stalled response body or a fetch implementation that does
    // not settle when aborted. Neither promise can reject into a render.
    return await Promise.race([response, timeout])
  } catch {
    return { kind: 'error', reason: 'request' }
  } finally {
    clearTimeout(timer)
  }
}

export async function listEngineChanges(input = {}) {
  try {
    const { limit = 50, before, ...options } = input || {}
    const query = new URLSearchParams({ limit: String(Math.max(1, Math.min(100, Math.floor(Number(limit) || 50)))) })
    if (typeof before === 'string' && before) query.set('before', before)
    const result = await request(`/api/engine-changes?${query}`, 'GET', options)
    if (result.kind !== 'ok') return result
    const { cards, unread_count, next_cursor } = result.data
    if (!Array.isArray(cards) || !Number.isInteger(unread_count) || unread_count < 0) {
      return { kind: 'error', reason: 'response' }
    }
    return { kind: 'ok', cards, unread_count, next_cursor: next_cursor || null }
  } catch {
    return { kind: 'error', reason: 'request' }
  }
}

async function cardRequest(cardId, suffix, method, options) {
  try {
    if (typeof cardId !== 'string' || !cardId) return { kind: 'error', reason: 'invalid-card' }
    const result = await request(`/api/engine-changes/${encodeURIComponent(cardId)}${suffix}`, method, options)
    return result.kind === 'ok' ? { kind: 'ok', card: result.data.card || result.data } : result
  } catch {
    return { kind: 'error', reason: 'invalid-card' }
  }
}

export function getEngineChange(cardId, options) {
  return cardRequest(cardId, '', 'GET', options)
}

export function markEngineChangeRead(cardId, options) {
  return cardRequest(cardId, '/read', 'POST', options)
}

export function requestEngineChangeHold(cardId, options) {
  return cardRequest(cardId, '/hold-request', 'POST', options)
}

function discussionField(value, max) {
  if (typeof value !== 'string' && typeof value !== 'number') return 'Not recorded'
  const text = String(value).replace(/\s+/g, ' ').trim()
  // Check the whole value BEFORE shortening: truncating a credential's shape
  // first could send its prefix. Do not copy auth fields or serialize a card.
  // The transport seam guards the sent turn.
  if (/\bbearer\s+\S+|\b(?:token|secret|password|api[_-]?key)\s*[:=]/i.test(text)) {
    return '[credential omitted]'
  }
  if (text.startsWith('{') || text.startsWith('[')) {
    try {
      const value = JSON.parse(text)
      if (value && typeof value === 'object') return '[structured value omitted]'
    } catch { /* Ordinary prose can start with a bracket. */ }
  }
  return text ? text.slice(0, max) : 'Not recorded'
}

export function engineChangeDiscussText(card) {
  const c = card || {}
  return [
    `Discuss engine change: ${discussionField(c.title, 180)}`,
    `State: ${discussionField(c.state, 24)}`,
    `Feature: ${discussionField(c.feature_id, 100)}`,
    `What broke / what changed: ${discussionField(c.summary, 700)}`,
    `PR: ${discussionField(c.change?.pr_number, 24)}`,
    `Head SHA: ${discussionField(c.change?.head_sha, 64)}`,
    `Regression spec: ${discussionField(c.evidence?.regression_spec, 220)}`,
  ].join('\n').slice(0, 1500)
}
