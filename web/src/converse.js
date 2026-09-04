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
import { trackErrorShown, trackStreamDown } from './telemetry.js'
// The credential guard rides the WIRE (slice 8a round 3): postMessage is the
// endpoint every conversational surface shares, so it carries the guard rather
// than each of its four callers. See lib/secretGuardTransport.js.
import { SecretRefusedError, guardedText } from './lib/secretGuardTransport.js'

const API_BASE = config.apiBase
const TENANT = config.tenant

// Two-tier dispatch thresholds (wire contract §11) — the ONE place they live.
//   confidence >= CHIP_ONLY (lane run)  -> RoutePanel chip only (today's UX)
//   RACE_MIN..CHIP_ONLY (lane run)      -> chip AND an agent turn race
//   below RACE_MIN / build / solve      -> ConversePanel primary
export const THRESHOLDS = { CHIP_ONLY: 0.80, RACE_MIN: 0.55 }

const EMPTY_ACTIVITY = Object.freeze({ queued: 0, executing: 0, total: 0 })

export function projectActivityProjection(value) {
  if (!value || typeof value !== 'object') return EMPTY_ACTIVITY
  const queued = Math.max(0, Number(value.queued) || 0)
  const executing = Math.max(0, Number(value.executing) || 0)
  const total = Math.max(queued + executing, Number(value.total) || 0)
  return { queued, executing, total }
}

// §3 SSE event vocabulary (event name = type; each data line is the full
// {v, session_id, turn_id, seq, type, data} envelope).
// EVERY type the server can emit must appear here. EventSource dispatches by
// name, so a type without a listener below is dropped in total silence — no
// error, no warning. It then arrives only if the transcript poll happens to
// win a race, which is exactly how `question_required` and the two queue
// events shipped as intermittent bugs. test_contract_freeze.py pins this list
// against the server's S18_STREAM_TYPES so the next addition cannot repeat it.
export const STREAM_EVENT_TYPES = [
  'turn_started', 'text_delta', 'tool_call', 'tool_result', 'job_linked',
  'proposed_run', 'confirmation_required', 'question_required', 'confirmation_resolved',
  'turn_usage', 'turn_complete', 'session_state', 'error',
  // Durable queue events (turn_runner appends them; composer.js reads them).
  'turn_queued', 'turn_queue_dropped',
  // T1 overlay lifecycle (server/overlay_stream.py). `overlay_revoked` is the
  // one a user must never miss — missing it leaves a withdrawn theme on screen.
  'overlay_proposed', 'overlay_decided', 'overlay_revoked',
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
  // P2 auto-capture: the agent-wire seam every user-visible chat error
  // crosses (the api.js http() seam covers the rest).
  trackErrorShown({
    http_status: res.status,
    error_code: e.errorCode || undefined,
    endpoint_class: '/api/sessions',
    ui_class: 'agent',
  })
  return e
}

async function post(path, payload) {
  const headers = { 'Content-Type': 'application/json', 'X-Tenant-Id': TENANT, ...authHeaders() }
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  })
  const body = await res.json().catch(() => null)
  const code = String(body?.error?.error_code || '').toLowerCase()
  const grantRequired = res.status === 401 && (code === 'grant_required' || body?.grant_required === true)
  if (!grantRequired) noteUnauthorized(res, path, headers.Authorization)
  return { res, body }
}

async function get(path) {
  const headers = { 'X-Tenant-Id': TENANT, ...authHeaders() }
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'GET',
    headers,
  })
  const body = await res.json().catch(() => null)
  noteUnauthorized(res, path, headers.Authorization)
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

// --- Conversation scope (standardization slice 6b) ------------------------
// The CLOSED kind set. Pinned byte-equal to server/session_store.py SCOPE_KINDS
// and routers/sessions.py by test_contract_freeze.py, exactly as
// STREAM_EVENT_TYPES is: an addition here that never reaches the server is a
// 422 the user reads as "the conversation would not start".
export const SCOPE_KINDS = ['project', 'drawing', 'entity']
// A handle is an opaque identifier (drawing id, project uuid, entity handle).
// Bounded HERE too, not only at the wire: a client that would post an
// over-long handle gets a local `null` scope and falls back to the legacy
// identity instead of a round trip that can only 422.
export const SCOPE_HANDLE_MAX = 128
// The drawing key a project-scoped attach carries when it names no drawing.
// The same sentinel the cache key has always used and the same one
// routers/sessions.py DEFAULT_SCOPE_DRAWING_ID writes, so both ends agree on
// which session a project conversation with no drawing is.
const DEFAULT_DRAWING_KEY = 'default'

/**
 * `{kind, handle}` (plus an optional client-side `drawingId` for the kinds
 * that live inside a drawing) -> the same shape, validated; anything else ->
 * null. Pure, allocation-light, and FAILS CLOSED: an unknown kind, a missing,
 * non-string, empty or over-long handle all return null rather than a
 * half-formed scope that would reach the wire.
 */
export function normalizeScope(value) {
  if (!value || typeof value !== 'object') return null
  const { kind, handle } = value
  if (!SCOPE_KINDS.includes(kind)) return null
  if (typeof handle !== 'string') return null
  const trimmed = handle.trim()
  if (!trimmed || trimmed.length > SCOPE_HANDLE_MAX) return null
  const scope = { kind, handle: trimmed }
  // `drawingId` is CLIENT-SIDE ONLY: the wire scope is exactly {kind, handle}
  // (the server model forbids extra keys), and the drawing rides the body's
  // own `drawing_id` field. An entity scope names an entity INSIDE a drawing,
  // so the server refuses one without a drawing_id — carrying it here is what
  // lets a caller pass one object instead of two arguments.
  if (typeof value.drawingId === 'string' && value.drawingId) {
    scope.drawingId = value.drawingId
  }
  return scope
}

/** The (drawingId, projectId) a scope resolves to on today's wire. */
function identityOfScope(scope) {
  if (scope.kind === 'project') {
    return { drawingId: scope.drawingId || DEFAULT_DRAWING_KEY, projectId: scope.handle }
  }
  if (scope.kind === 'drawing') return { drawingId: scope.handle, projectId: null }
  return { drawingId: scope.drawingId || null, projectId: null } // entity
}

// --- Session create/attach (idempotent per tenant+drawing) ----------------
// POST /api/sessions is idempotent server-side; the cache here only saves the
// round-trip on repeat dispatches. A failed create never sticks, and callers
// drop a stale entry via resetSession when a message 404s (harness restarted).
const sessionCache = new Map() // project+drawing -> Promise<{session_id, status, created_at}>

/**
 * The cache key for one conversation. TWO accepted call shapes, ONE template
 * family, so slice 6b widened the signature without moving a single existing
 * key string:
 *
 *   sessionCacheKey(drawingId, projectId?)   the legacy positional form
 *   sessionCacheKey({kind, handle, drawingId?})   the scope form
 *
 * A `drawing` scope keys exactly like the bare drawing it names, and a
 * `project` scope keys exactly like today's project+drawing pair, so a client
 * that switches from the positional form to the scope form ATTACHES TO THE
 * SAME cached session rather than opening a second one. `entity` gets its own
 * prefix because two entity conversations in one drawing are two
 * conversations. An unnormalizable scope object falls back to the 'default'
 * key rather than keying on `[object Object]`.
 */
export function sessionCacheKey(drawingIdOrScope, projectId = null) {
  if (drawingIdOrScope && typeof drawingIdOrScope === 'object') {
    const scope = normalizeScope(drawingIdOrScope)
    if (!scope) return DEFAULT_DRAWING_KEY
    const identity = identityOfScope(scope)
    const drawingKey = identity.drawingId || DEFAULT_DRAWING_KEY
    if (scope.kind === 'entity') return `entity:${scope.handle}:drawing:${drawingKey}`
    return identity.projectId
      ? `project:${identity.projectId}:drawing:${drawingKey}`
      : drawingKey
  }
  const drawingKey = drawingIdOrScope || DEFAULT_DRAWING_KEY
  return projectId ? `project:${projectId}:drawing:${drawingKey}` : drawingKey
}

async function createSession(drawingIdOrScope, projectId = null) {
  const scope = drawingIdOrScope && typeof drawingIdOrScope === 'object'
    ? normalizeScope(drawingIdOrScope)
    : null
  // The LEGACY body is unchanged, byte for byte, when no scope was passed:
  // `scope` is emitted ONLY for a caller that asked for one, so every deployed
  // caller and every recorded wire fixture keeps its exact payload.
  const identity = scope ? identityOfScope(scope) : { drawingId: drawingIdOrScope, projectId }
  const payload = { drawing_id: identity.drawingId }
  if (identity.projectId) payload.project_id = identity.projectId
  if (scope) payload.scope = { kind: scope.kind, handle: scope.handle }
  const { res, body } = await post('/api/sessions', payload)
  if (!res.ok || !body || !body.session_id) {
    throw tagged(res, body, `POST /api/sessions -> ${res.status}`)
  }
  return body
}

export function ensureSession(drawingIdOrScope, projectId = null) {
  const key = sessionCacheKey(drawingIdOrScope, projectId)
  if (!sessionCache.has(key)) {
    const p = createSession(drawingIdOrScope, projectId).catch((e) => {
      sessionCache.delete(key)
      throw e
    })
    sessionCache.set(key, p)
  }
  return sessionCache.get(key)
}

export function resetSession(drawingIdOrScope, projectId = null) {
  sessionCache.delete(sessionCacheKey(drawingIdOrScope, projectId))
}

// --- The tenant's conversations (GET /api/sessions, slice 6b) -------------
//: One page is at most this many rows. The server clamps to the same figure;
//: asking for more is a slower query for a page the server will trim anyway.
export const CONVERSATION_PAGE_MAX = 50

/**
 * One bounded page of the CALLER'S OWN conversations, newest first.
 *
 * `scope` narrows to one conversation scope (omit for every conversation of
 * the tenant); `limit` is clamped to [1, CONVERSATION_PAGE_MAX] before it
 * reaches the wire; `cursor` is the previous page's `next_cursor`. Throws
 * tagged like every other call here, so ConversationList can classify the
 * failure instead of string-matching it. Never returns a partial page as if it
 * were the whole list: a non-2xx is an error, not an empty array.
 */
export async function listSessions({ scope = null, limit = 20, cursor = null } = {}) {
  const params = new URLSearchParams()
  const normalized = normalizeScope(scope)
  if (normalized) params.set('scope', `${normalized.kind}:${normalized.handle}`)
  const bounded = Math.max(1, Math.min(Number(limit) || 1, CONVERSATION_PAGE_MAX))
  params.set('limit', String(bounded))
  if (typeof cursor === 'string' && cursor) params.set('cursor', cursor)
  const path = `/api/sessions?${params.toString()}`
  const { res, body } = await get(path)
  if (!res.ok || !body) throw tagged(res, body, `GET ${path} -> ${res.status}`)
  return {
    sessions: Array.isArray(body.sessions) ? body.sessions : [],
    nextCursor: typeof body.next_cursor === 'string' && body.next_cursor
      ? body.next_cursor
      : null,
  }
}

// --- Start a turn ---------------------------------------------------------
// POST /api/sessions/{id}/messages — exactly one of a user message (text
// and/or images) or confirm (wire §2).
// 202 {turn_id, status:"started"}; everything else throws tagged (409
// turn_in_progress · 401 grant_required · 429 llm_quota_exhausted /
// llm_rate_limited · 404 session_not_found).
export async function postMessage(sessionId, {
  text, confirm, images, classifier_hint, credential_grant, queue, request_id,
  // Slice 6b hand-off identity: WHO asked for this background start. Bounded
  // and trimmed below before it can reach the wire; slice 11 is its consumer
  // (the build queue card), and nothing in this slice reads it back.
  requested_by,
  // Per-call authorisation for an OVERRIDABLE refusal only, passed in by the
  // composer whose "Send anyway" the user just clicked. It is read here and
  // never forwarded onto the wire, and nothing stores it: the next call starts
  // from refused again. See lib/secretGuardTransport.js.
  allowSecretOnce = false,
} = {}) {
  // GUARDED TRANSPORT. This endpoint is where user text becomes model context,
  // and it has FOUR client callers: the assistant reply box, the catalog
  // controller's agent turn, the Author-a-tool authority mint on both shells,
  // and this module's own confirm/resume. Guarding it here is what makes the
  // count irrelevant.
  if (text != null) {
    const guard = guardedText(text, { allowSecretOnce })
    if (!guard.ok) throw new SecretRefusedError(guard.refusal)
  }
  const payload = {}
  if (text != null) payload.text = text
  if (confirm != null) payload.confirm = confirm
  if (images != null) payload.images = images
  if (classifier_hint != null) payload.classifier_hint = classifier_hint
  if (credential_grant != null) payload.credential_grant = credential_grant
  if (queue === true) payload.queue = true
  if (request_id != null) payload.request_id = request_id
  // Only a QUEUED start is a background hand-off, and only a bounded non-empty
  // string rides along: an over-long or blank value is dropped here rather
  // than posted for the server to refuse. Never logged, never echoed.
  if (queue === true && typeof requested_by === 'string') {
    const who = requested_by.trim()
    if (who && who.length <= SCOPE_HANDLE_MAX) payload.requested_by = who
  }
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
    const headers = { 'X-Tenant-Id': TENANT, ...authHeaders() }
    const res = await fetch(`${API_BASE}/api/converse/registry`, { headers })
    noteUnauthorized(res, '/api/converse/registry', headers.Authorization)
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
    const headers = { 'X-Tenant-Id': TENANT, ...authHeaders() }
    const res = await fetch(`${API_BASE}/api/skills`, { headers })
    noteUnauthorized(res, '/api/skills', headers.Authorization)
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

// Human-only standard-service approval. The app resolves the stored broker
// identity, mints both short-lived credentials server-side, executes the exact
// stored call through the private harness host, and returns only a safe receipt.
export async function approveStandardService(approvalId, argumentDigest) {
  const { res, body } = await post('/api/mcp/gateway/approvals/execute', {
    approval_id: approvalId,
    argument_digest: argumentDigest,
  })
  if (!res.ok) {
    throw tagged(res, body, `POST /api/mcp/gateway/approvals/execute -> ${res.status}`)
  }
  return body
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
      trackStreamDown(1)  // P2: streaming reliability as users feel it (capped 10/session)
      reconnectTimer = setTimeout(openEs, retryMs)
      retryMs = Math.min(retryMs * 2, 10000)
    }
  }

  const pollOnce = async () => {
    if (closed) return
    try {
      for (;;) {
        if (closed) return
        const headers = { 'X-Tenant-Id': TENANT, ...authHeaders() }
        const res = noteUnauthorized(await fetch(
          `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/transcript?limit=${pollLimit}`,
          { headers },
        ), `/api/sessions/${sessionId}/transcript`, headers.Authorization)
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
