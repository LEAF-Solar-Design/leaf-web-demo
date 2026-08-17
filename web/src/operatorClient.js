/**
 * Operator console client (contract/OPERATOR.md). Mirrors api.js/overlayClient.js
 * conventions on purpose: same API_BASE, same authHeaders() bearer-only auth.
 *
 * SECURITY INVARIANT (Wave 2 Lane E acceptance #1 / security test): this file
 * NEVER sets the operator subject/profile override headers (the X- pair read
 * by server/operator_deps.py as a DEV convenience when auth is off; the
 * security test greps this file and fails on their literal names, so they are
 * deliberately not spelled here). The live grant is the JWT subject plus the
 * server-owned operator_principals row. A browser that could set its own
 * operator subject would be minting its own authority, which
 * contract/OPERATOR.md section 1 forbids outright. If you are tempted to add
 * either header here to "make local dev easier", don't — every operator route
 * resolves the principal from the bearer token alone.
 *
 * Every operator route answers 404 (no existence oracle) for an absent surface,
 * a denied principal, AND an unknown resource — indistinguishable on purpose.
 * 401/403/404 from ANY operator call means "stop trusting what you have
 * cached" (acceptance #4): every call here funnels through operatorFetch,
 * which fires the signed-out listeners on those three statuses so the console
 * can reset to a safe state with no retry loop.
 */

import { config, authHeaders } from './api.js'

const API_BASE = config.apiBase

const DENIED_STATUSES = new Set([401, 403, 404])

const signedOutListeners = new Set()

export function subscribeOperatorSignedOut(listener) {
  signedOutListeners.add(listener)
  return () => signedOutListeners.delete(listener)
}

function notifySignedOut(source) {
  for (const listener of signedOutListeners) {
    try { listener(source) } catch { /* a listener must not break the transport */ }
  }
}

export function isOperatorDenied(err) {
  return !!err && DENIED_STATUSES.has(err.status)
}

function tagged(status, body, fallback) {
  const e = new Error((body && (body.detail || body.error)) || fallback)
  e.status = status
  e.body = body
  return e
}

/**
 * The single seam every operator call rides. Headers are Authorization-only
 * (see the file banner) — no operator identity header is ever attached from
 * the browser. On 401/403/404 this fires the signed-out listeners BEFORE
 * throwing, so a caller's catch block and the console's global reset both see
 * it in the same tick.
 */
async function operatorFetch(path, opts = {}) {
  const headers = { ...(opts.headers || {}), ...authHeaders() }
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers })
  const body = await res.json().catch(() => null)
  if (DENIED_STATUSES.has(res.status)) notifySignedOut(path)
  if (!res.ok) throw tagged(res.status, body, `${opts.method || 'GET'} ${path} -> ${res.status}`)
  return body
}

function postJson(path, payload) {
  return operatorFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  })
}

// --- Probe (acceptance #6): the ONLY call App.jsx makes before mounting the
// entry, and it makes it exactly once. Cached at module scope so a second
// mount (e.g. React.StrictMode's dev double-invoke) reuses the same in-flight
// or settled promise instead of issuing a second request.
let probePromise = null

export function resetOperatorProbeForTests() {
  probePromise = null
}

export function probeOperatorConsole() {
  if (!probePromise) {
    probePromise = (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/operator/sessions`, { headers: { ...authHeaders() } })
        return !DENIED_STATUSES.has(res.status) && res.status !== 503
      } catch {
        return false
      }
    })()
  }
  return probePromise
}

// --- Sessions (server/routers/operator_sessions.py) ----------------------
export function createSession() {
  return postJson('/api/operator/sessions', {})
}

export function listSessions() {
  return operatorFetch('/api/operator/sessions').then((body) => body.sessions || [])
}

export function getSession(sessionId) {
  return operatorFetch(`/api/operator/sessions/${encodeURIComponent(sessionId)}`)
}

export function getEvents(sessionId, afterSeq = 0, limit = 200) {
  return operatorFetch(
    `/api/operator/sessions/${encodeURIComponent(sessionId)}/events?after_seq=${afterSeq}&limit=${limit}`,
  ).then((body) => body.events || [])
}

export function postMessage(sessionId, text) {
  return postJson(`/api/operator/sessions/${encodeURIComponent(sessionId)}/messages`, { text })
}

export function getAudit(limit = 100) {
  return operatorFetch(`/api/operator/audit?limit=${limit}`).then((body) => body.audit || [])
}

// --- Runbook: tenant-agent pause/resume (server/routers/operator_runbooks.py)
export function tenantAgentState(tenantId) {
  return operatorFetch(`/api/operator/runbooks/tenant-agent/${encodeURIComponent(tenantId)}/state`)
}
export function tenantAgentPropose(verb, tenantId) {
  return postJson(`/api/operator/runbooks/tenant-agent/${encodeURIComponent(verb)}/propose`, { tenant_id: tenantId })
}
export function tenantAgentExecute(verb, tenantId, authorityId) {
  return postJson(`/api/operator/runbooks/tenant-agent/${encodeURIComponent(verb)}/execute`,
    { tenant_id: tenantId, authority_id: authorityId })
}

// --- Runbook: tenant overlay (server/routers/operator_overlay.py) --------
export function tenantOverlayState(tenantId) {
  return operatorFetch(`/api/operator/runbooks/tenant-overlay/${encodeURIComponent(tenantId)}/state`)
}
export function tenantOverlayPropose(tenantId, overlay) {
  return postJson('/api/operator/runbooks/tenant-overlay/propose', { tenant_id: tenantId, overlay })
}
export function tenantOverlayExecute(tenantId, overlay, authorityId) {
  return postJson('/api/operator/runbooks/tenant-overlay/execute',
    { tenant_id: tenantId, overlay, authority_id: authorityId })
}

// --- Runbook: credential rotation (server/routers/operator_credential.py) --
export function credentialState(handle) {
  return operatorFetch(`/api/operator/runbooks/credential/${encodeURIComponent(handle)}/state`)
}
export function credentialPropose(handle) {
  return postJson('/api/operator/runbooks/credential/propose', { credential_handle: handle })
}
export function credentialExecute(handle, authorityId) {
  return postJson('/api/operator/runbooks/credential/execute',
    { credential_handle: handle, authority_id: authorityId })
}

// --- Runbook: external write (server/routers/operator_external.py) -------
export function externalDestinations() {
  return operatorFetch('/api/operator/external/destinations').then((body) => body.destinations || [])
}
export function externalPropose(destination, tokenHandle, adapter, payload) {
  return postJson('/api/operator/external/propose',
    { destination, token_handle: tokenHandle, adapter, payload: payload ?? null })
}
export function externalExecute(destination, tokenHandle, adapter, authorityId, payload) {
  return postJson('/api/operator/external/execute',
    { destination, token_handle: tokenHandle, adapter, authority_id: authorityId, payload: payload ?? null })
}

// --- Runbook: stage release candidate (server/routers/operator_release.py) -
export function releaseState(target, sourceSha) {
  return operatorFetch(
    `/api/operator/release/${encodeURIComponent(target)}/${encodeURIComponent(sourceSha)}/state`,
  )
}
export function releasePropose(sourceSha, target) {
  return postJson('/api/operator/release/propose', { source_sha: sourceSha, target })
}
export function releaseExecute(sourceSha, target, authorityId) {
  return postJson('/api/operator/release/execute',
    { source_sha: sourceSha, target, authority_id: authorityId })
}

// --- Secrets (server/routers/operator_secrets.py) — handle metadata ONLY.
// Never a value; no route here can ever return one.
export function listSecretHandles() {
  return operatorFetch('/api/operator/secrets').then((body) => body.handles || [])
}
export function describeSecret(handle) {
  return operatorFetch(`/api/operator/secrets/${encodeURIComponent(handle)}`)
}

// --- Worker dispatch (server/routers/operator_worker.py) -----------------
// CAPABILITY-ONLY: there is no GET status/list route and no cancel route on
// main. The console can only show the receipt from the dispatch it made.
export function dispatchWorker(commands, { repo = null, timeoutMs = null } = {}) {
  const body = { commands }
  if (repo != null) body.repo = repo
  if (timeoutMs != null) body.timeout_ms = timeoutMs
  return postJson('/api/operator/worker/dispatch', body)
}
