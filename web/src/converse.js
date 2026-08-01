// Conversational agent lane (§18) — the web client for server/routers/sessions.py.
// Mirrors api.js conventions exactly: same API_BASE / X-Tenant-Id / authHeaders
// on every call, errors tagged with .status/.body so callers gate without
// string-matching, and the same belt-and-suspenders stream pattern subscribeJob
// uses (EventSource for low latency PLUS a poll fallback — here the durable
// transcript, deduped by seq, so replay after a reconnect is exact).
//
// This lane is ADDITIVE: the deterministic §12 prompt router stays the floor.
// LIVE only — mock mode never imports a session (the agent tier is disabled
// entirely in VITE_MOCK; see App.jsx onDispatch).

import { config, authHeaders, noteUnauthorized } from './api.js'

const API_BASE = config.apiBase
const TENANT = config.tenant

// Two-tier dispatch thresholds (wire contract §11) — the ONE place they live.
//   confidence >= CHIP_ONLY (lane run)  -> RoutePanel chip only (today's UX)
//   RACE_MIN..CHIP_ONLY (lane run)      -> chip AND an agent turn race
//   below RACE_MIN / build / solve      -> ConversePanel primary
export const THRESHOLDS = { CHIP_ONLY: 0.80, RACE_MIN: 0.55 }

// §3 SSE event vocabulary (event name = type; each data line is the full
// {v, session_id, turn_id, seq, type, data} envelope).
export const STREAM_EVENT_TYPES = [
  'turn_started', 'text_delta', 'tool_call', 'tool_result', 'job_linked',
  'proposed_run', 'confirmation_required', 'confirmation_resolved',
  'turn_usage', 'turn_complete', 'session_state', 'error',
]

// Tag a non-2xx into an Error carrying the §10 body, like api.js authorTool
// does — callers classify via classifyAgentError, never by message text.
function tagged(res, body, fallback) {
  const e = new Error(
    (body && body.error && (body.error.message || body.error.error_code)) || fallback,
  )
  e.status = res.status
  e.body = body
  e.errorCode = (body && body.error && body.error.error_code) || null
  e.degraded = !!(body && body.degraded_mode)
  return e
}

async function post(path, payload) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT, ...authHeaders() },
    body: JSON.stringify(payload),
  })
  const body = await res.json().catch(() => null)
  const code = String(body?.error?.error_code || '').toLowerCase()
  const grantRequired = res.status === 401 && (code === 'grant_required' || body?.grant_required === true)
  if (!grantRequired) noteUnauthorized(res, path)
  return { res, body }
}

async function get(path) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'GET',
    headers: { 'X-Tenant-Id': TENANT, ...authHeaders() },
  })
  const body = await res.json().catch(() => null)
  noteUnauthorized(res, path)
  return { res, body }
}

// Coarse error classification for the calm degraded surfaces (wire §8 codes
// are lowercase on the wire, like the existing quota_exceeded).
export function classifyAgentError(err) {
  const code = String(err?.errorCode || '').toLowerCase()
  if (code === 'llm_quota_exhausted') return 'quota'
  if (code === 'llm_rate_limited') return 'rate_limited'
  if (code === 'turn_in_progress') return 'busy'
  if (code === 'session_not_found') return 'not_found'
  // Approval-lifecycle errors get their own kinds (both arrive as BAD_PARAMS,
  // so the status is the discriminator; 409 turn_in_progress is already
  // caught by errorCode above): 409 = routers/agent.py "approval not
  // resolvable" (record already decided — retry-safe for the §7 confirm
  // message), 410 = routers/sessions.py "confirmation expired" (TTL lapsed —
  // only a fresh proposal can proceed).
  if (err?.status === 410) return 'confirmation_expired'
  if (err?.status === 409 && code === 'bad_params') return 'approval_stale'
  // 413 is also BAD_PARAMS, and falling through to 'unreachable' told the user
  // we could not reach the assistant when the assistant had answered clearly:
  // the payload is too big. That is a thing the user can act on (send fewer or
  // smaller images), and an outage is not.
  if (err?.status === 413) return 'too_large'
  if (err?.status === 401 || code === 'grant_required' || !!(err?.body && err.body.grant_required)) return 'grant'
  if (err?.status === 403 || code === 'entitlement_required' || !!(err?.body && err.body.entitlement_required)) return 'entitlement'
  return 'unreachable'
}

// --- Session create/attach (idempotent per tenant+drawing) ----------------
// POST /api/sessions is idempotent server-side; the cache here only saves the
// round-trip on repeat dispatches. A failed create never sticks, and callers
// drop a stale entry via resetSession when a message 404s (harness restarted).
const sessionCache = new Map() // drawingId -> Promise<{session_id, status, created_at}>

async function createSession(drawingId) {
  const { res, body } = await post('/api/sessions', { drawing_id: drawingId })
  if (!res.ok || !body || !body.session_id) {
    throw tagged(res, body, `POST /api/sessions -> ${res.status}`)
  }
  return body
}

export function ensureSession(drawingId) {
  const key = drawingId || 'default'
  if (!sessionCache.has(key)) {
    const p = createSession(drawingId).catch((e) => {
      sessionCache.delete(key)
      throw e
    })
    sessionCache.set(key, p)
  }
  return sessionCache.get(key)
}

export function resetSession(drawingId) {
  sessionCache.delete(drawingId || 'default')
}

// --- Start a turn ---------------------------------------------------------
// POST /api/sessions/{id}/messages — exactly one of a user message (text
// and/or images) or confirm (wire §2).
// 202 {turn_id, status:"started"}; everything else throws tagged (409
// turn_in_progress · 401 grant_required · 429 llm_quota_exhausted /
// llm_rate_limited · 404 session_not_found).
export async function postMessage(sessionId, {
  text, confirm, images, classifier_hint, credential_grant, queue,
} = {}) {
  const payload = {}
  if (text != null) payload.text = text
  if (confirm != null) payload.confirm = confirm
  if (images != null) payload.images = images
  if (classifier_hint != null) payload.classifier_hint = classifier_hint
  if (credential_grant != null) payload.credential_grant = credential_grant
  if (queue === true) payload.queue = true
  const { res, body } = await post(
    `/api/sessions/${encodeURIComponent(sessionId)}/messages`, payload,
  )
  if (res.status === 202 && body && (body.turn_id || body.status === 'queued')) return body
  throw tagged(res, body, `POST /api/sessions/${sessionId}/messages -> ${res.status}`)
}

// --- Slash-menu registry --------------------------------------------------
// Commands + skills + tools in one tenant-scoped catalog (server:
// converse_registry.build_registry). Every entry carries `kind` — which is
// what web/src/composer.js rankEntries groups on — and `client_action`, which
// filterRunnable uses to drop anything this client cannot dispatch.
//
// Resolves to an empty list on ANY failure: the picker degrades to the tools
// the catalog lane already supplies rather than the composer erroring. A menu
// is not worth breaking the input over.
export async function fetchRegistry() {
  try {
    const res = await fetch(`${API_BASE}/api/converse/registry`, {
      headers: { 'X-Tenant-Id': TENANT, ...authHeaders() },
    })
    noteUnauthorized(res, '/api/converse/registry')
    if (!res.ok) return { entries: [], counts: {} }
    const body = await res.json().catch(() => null)
    const entries = Array.isArray(body?.entries) ? body.entries : []
    return { entries, counts: body?.counts || {} }
  } catch {
    return { entries: [], counts: {} }
  }
}

// The standalone skills catalog is a fallback source for the slash picker.
// It must never make the composer fail when an older deployment lacks it.
export async function fetchSkills() {
  try {
    const res = await fetch(`${API_BASE}/api/skills`, {
      headers: { 'X-Tenant-Id': TENANT, ...authHeaders() },
    })
    noteUnauthorized(res, '/api/skills')
    if (!res.ok) return { skills: [] }
    const body = await res.json().catch(() => null)
    return { skills: Array.isArray(body?.skills) ? body.skills : [] }
  } catch {
    return { skills: [] }
  }
}

// --- Interrupt (Esc / Stop) -----------------------------------------------
// Ends the session's ACTIVE turn. The server terminalizes it as
// `turn_complete{stop_reason:'interrupted'}` — an event this client already
// renders — so nothing new arrives on the wire and the transcript keeps the
// work done so far.
//
// A 409 means the turn already ended on its own (it is no longer the active
// turn) — the user's intent is satisfied either way, so this resolves rather
// than throwing: an interrupt that races completion is not an error the user
// should see.
export async function cancelTurn(sessionId, turnId) {
  const { res, body } = await post(
    `/api/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}/cancel`,
    {},
  )
  if (res.status === 202) return body || { turn_id: turnId, status: 'cancelled' }
  if (res.status === 409) return { turn_id: turnId, status: 'already_ended' }
  throw tagged(res, body, `POST /api/sessions/${sessionId}/turns/${turnId}/cancel -> ${res.status}`)
}

// --- Split-turn approval (wire §7) ----------------------------------------
// Step (a) only: record the decision. The caller then posts the confirm
// MESSAGE (postMessage with {confirm}) to start the resume turn — the server
// deliberately never auto-starts it.
export async function approve(confirmationId, approved) {
  const { res, body } = await post(
    `/api/agent/approvals/${encodeURIComponent(confirmationId)}`,
    { approved: !!approved },
  )
  if (!res.ok) throw tagged(res, body, `POST /api/agent/approvals/${confirmationId} -> ${res.status}`)
  return body || { resolved: true, approved: !!approved }
}

export async function listPendingApprovals(sessionId, limit = 100) {
  const bounded = Math.max(1, Math.min(Number(limit) || 100, 100))
  const query = `session_id=${encodeURIComponent(sessionId)}&limit=${bounded}`
  const { res, body } = await get(`/api/agent/approvals/pending?${query}`)
  if (!res.ok) throw tagged(res, body, `GET /api/agent/approvals/pending -> ${res.status}`)
  return Array.isArray(body?.approvals) ? body.approvals : []
}

export async function resolveApproval(
  confirmationId, owningSessionId, approved, decisionRecorded = false,
) {
  if (!decisionRecorded) {
    await approve(confirmationId, approved)
  }
  return postMessage(owningSessionId, {
    confirm: { confirmationId, approved: !!approved },
  })
}

// --- Event stream ---------------------------------------------------------
// EventSource on GET /api/sessions/{id}/stream?after_seq=N with automatic
// reconnect (replaying from the last seen seq — the harness replays from
// sessions.db), PLUS a transcript poll fallback that works when SSE is
// blocked. Both channels feed one seq-deduped deliver(), so overlap is safe
// and events arrive exactly once, in order.
//
// EventSource cannot carry headers, so like the job stream it rides the
// server's default tenant resolution; the poll fallback sends the full
// X-Tenant-Id + auth headers (same documented limitation as api.js:427-489).
//
// handlers: { [eventType]: (data, envelope) => {}, onEvent(envelope),
//             onStreamDown() } — all optional. Returns { close(), seq }.
// Poll fallback window (the transcript route returns the most recent N events,
// ascending, with no after_seq cursor): starts cheap, widens when a gap is
// detected, resets once caught up.
const POLL_LIMIT = 200
const POLL_LIMIT_MAX = 10000

export function openStream(sessionId, afterSeq = 0, handlers = {}) {
  let closed = false
  let lastSeq = Number(afterSeq) || 0
  let es = null
  let reconnectTimer = null
  let retryMs = 1000
  let pollTimer = null
  let pollLimit = POLL_LIMIT

  const deliver = (envelope) => {
    if (closed || !envelope || !envelope.type) return
    const seq = Number(envelope.seq)
    if (Number.isFinite(seq)) {
      if (seq <= lastSeq) return // already seen (SSE/poll overlap or replay)
      lastSeq = seq
    }
    try {
      const h = handlers[envelope.type]
      if (h) h(envelope.data || {}, envelope)
      if (handlers.onEvent) handlers.onEvent(envelope)
    } catch { /* a handler exception must never kill the stream */ }
  }

  const parse = (raw) => {
    try { return JSON.parse(raw) } catch { return null }
  }

  const openEs = () => {
    if (closed || typeof EventSource === 'undefined') return
    try {
      es = new EventSource(
        `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/stream?after_seq=${lastSeq}`,
      )
    } catch { es = null; return } // construction failed; the poll covers it
    const onEvt = (ev) => {
      const env = parse(ev.data)
      if (env) { retryMs = 1000; deliver(env) }
    }
    for (const t of STREAM_EVENT_TYPES) es.addEventListener(t, onEvt)
    es.onmessage = onEvt // defensive: unnamed frames still carry the envelope
    es.onerror = () => {
      // Reconnect with after_seq replay; backoff caps at 10s. The transcript
      // poll keeps events flowing while the SSE leg is down.
      try { es.close() } catch { /* noop */ }
      es = null
      if (closed) return
      if (handlers.onStreamDown) handlers.onStreamDown()
      reconnectTimer = setTimeout(openEs, retryMs)
      retryMs = Math.min(retryMs * 2, 10000)
    }
  }

  const pollOnce = async () => {
    if (closed) return
    try {
      for (;;) {
        if (closed) return
        const res = noteUnauthorized(await fetch(
          `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/transcript?limit=${pollLimit}`,
          { headers: { 'X-Tenant-Id': TENANT, ...authHeaders() } },
        ), `/api/sessions/${sessionId}/transcript`)
        if (res.status === 401) {
          closed = true
          if (es) { try { es.close() } catch { /* noop */ } es = null }
          if (reconnectTimer) clearTimeout(reconnectTimer)
          if (pollTimer) clearInterval(pollTimer)
          return
        }
        if (!res.ok) return // transient / 404 — the SSE leg is authoritative
        const body = await res.json().catch(() => null)
        const events = (body && body.events) || [] // ascending seq
        // The transcript window is the most recent N only: a first seq beyond
        // lastSeq+1 means events between fell off the front of the window.
        // Delivering the tail anyway would advance lastSeq past the gap, and
        // the SSE reconnect (after_seq=lastSeq) could never replay it — widen
        // the window until it reaches lastSeq+1 instead. At the cap, deliver
        // the tail rather than nothing (bounded, and only past a 10k-event gap).
        const firstSeq = events.length ? Number(events[0].seq) : NaN
        if (Number.isFinite(firstSeq) && firstSeq > lastSeq + 1 && pollLimit < POLL_LIMIT_MAX) {
          pollLimit = Math.min(pollLimit * 4, POLL_LIMIT_MAX)
          continue
        }
        for (const env of events) deliver(env)
        pollLimit = POLL_LIMIT // caught up — back to the cheap window
        return
      }
    } catch { /* transient */ }
  }

  openEs()
  pollTimer = setInterval(pollOnce, 2500)
  pollOnce()

  return {
    close() {
      closed = true
      if (es) { try { es.close() } catch { /* noop */ } es = null }
      if (reconnectTimer) clearTimeout(reconnectTimer)
      if (pollTimer) clearInterval(pollTimer)
    },
    get seq() { return lastSeq },
  }
}
