// Product-event tracker — the client half of the P2 events layer
// (contract: server docs/PLATFORM_TELEMETRY.md; design: events_design.md).
//
// Discipline, copied from the server sink's contract:
//   * telemetry can NEVER break the product: every public function is
//     try/caught into a no-op; one retry max, then drop; bounded buffer.
//   * identity is SERVER-stamped: this module sends event names + labels +
//     a browser session UUID; the ingest door overwrites identity from the
//     verified principal and strips reserved keys.
//   * kill switch: VITE_TELEMETRY_DISABLED=1, or a missing fetch/endpoint,
//     turns every call into a no-op.
//
// Transport: buffer -> POST /api/telemetry at 20 events or 5 s (whichever
// first); navigator.sendBeacon on pagehide (the closeJobBeacon precedent).

// Dependency-free ON PURPOSE (design rule 7): api.js imports this module for
// the http()-seam auto-capture, so importing api.js back would be a cycle.
// The JWT key and API base are read directly (same values api.js uses).
const DISABLED = import.meta.env?.VITE_TELEMETRY_DISABLED === '1'
const API_BASE = import.meta.env?.VITE_API_BASE ?? ''
const FLUSH_AT = 20
const FLUSH_MS = 5000
const BUFFER_MAX = 200          // absolute cap; beyond it the oldest drop
const ERROR_CAP = 20            // error.shown per session (design cap)
const STREAM_DOWN_CAP = 10      // agent.stream_down per session (design cap)

const state = {
  buffer: [],
  timer: null,
  sessionId: null,
}

// Session-scoped caps survive reloads: the counter lives beside the session
// UUID in sessionStorage, WITH an in-memory floor so a throwing storage can
// never disable a cap (it degrades to per-load counting, still capped).
const memCaps = {}

function capCount(key) {
  const mem = memCaps[key] || 0
  try {
    return Math.max(mem, Number(sessionStorage.getItem(`leaf.telemetry.cap.${key}`)) || 0)
  } catch { return mem }
}

function capIncrement(key) {
  const next = capCount(key) + 1
  memCaps[key] = next
  try {
    sessionStorage.setItem(`leaf.telemetry.cap.${key}`, String(next))
  } catch { /* storage unavailable: the in-memory floor still enforces */ }
}

function sessionId() {
  if (state.sessionId) return state.sessionId
  try {
    const KEY = 'leaf.telemetry.session'
    let sid = sessionStorage.getItem(KEY)
    if (!sid) {
      sid = crypto.randomUUID()
      sessionStorage.setItem(KEY, sid)
    }
    state.sessionId = sid
  } catch {
    state.sessionId = 'none'
  }
  return state.sessionId
}

function endpoint() {
  return `${API_BASE}/api/telemetry`
}

function identityHeaders() {
  // Auth-off parity with every other client call: the ingest door resolves
  // this stub tenant exactly as api.js's calls do. Built OUTSIDE the storage
  // try so a throwing localStorage can never drop it.
  const out = {
    'X-Tenant-Id': import.meta.env?.VITE_TENANT_ID || 'demo-tenant',
  }
  try {
    const tok = localStorage.getItem('leaf.jwt')
    if (tok) out.Authorization = `Bearer ${tok}`
    const guest = localStorage.getItem('leaf.guest_session')
    if (guest) out['X-Guest-Session'] = guest
  } catch { /* storage unavailable: stub tenant header still applies */ }
  return out
}

function payload(events) {
  return JSON.stringify({
    schema_version: '1',
    session_id: sessionId(),
    events,
  })
}

async function post(events) {
  const res = await fetch(endpoint(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...identityHeaders() },
    body: payload(events),
    keepalive: true,
  })
  if (!res.ok) throw new Error(`telemetry ${res.status}`)
}

function flush() {
  try {
    if (state.timer) { clearTimeout(state.timer); state.timer = null }
    if (!state.buffer.length) return
    const events = state.buffer.splice(0, FLUSH_AT)
    post(events).catch(() => {
      // ONE retry PER BATCH, then drop — loss-tolerant by contract.
      setTimeout(() => { post(events).catch(() => {}) }, 2000)
    })
    if (state.buffer.length) schedule()
  } catch { /* telemetry never breaks the product */ }
}

function schedule() {
  try {
    if (state.timer) return
    state.timer = setTimeout(flush, FLUSH_MS)
  } catch { /* no-op */ }
}

/** Queue one event. `props` values should be primitives; the server
 * stringifies and bounds everything and strips reserved keys. */
export function track(name, props = {}, eventType = 'custom_event') {
  try {
    if (DISABLED || typeof fetch !== 'function') return
    state.buffer.push({
      event_type: eventType,
      event_name: name,
      client_ts: Date.now() / 1000,
      labels: props,
    })
    if (state.buffer.length > BUFFER_MAX) state.buffer.shift()
    if (state.buffer.length >= FLUSH_AT) flush()
    else schedule()
  } catch { /* telemetry never breaks the product */ }
}

/** Auto-capture for user-visible errors at the two transport seams
 * (api.js http(), converse.js tagged()); capped per session. */
export function trackErrorShown(props = {}) {
  try {
    if (capCount('error') >= ERROR_CAP) return
    capIncrement('error')
    track('error.shown', props, 'error')
  } catch { /* no-op */ }
}

/** Streaming reconnects, capped per session. */
export function trackStreamDown(reconnectsN) {
  try {
    if (capCount('stream_down') >= STREAM_DOWN_CAP) return
    capIncrement('stream_down')
    track('agent.stream_down', { reconnects_n: reconnectsN })
  } catch { /* no-op */ }
}

/** Crash capture (ErrorBoundary). */
export function trackException(props = {}) {
  try { track('client.exception', props, 'exception') } catch { /* no-op */ }
}

function beaconFlush() {
  try {
    if (DISABLED || !state.buffer.length) return
    const events = state.buffer.splice(0, FLUSH_AT)
    if (navigator?.sendBeacon) {
      // sendBeacon cannot carry auth headers; the pre-auth allowlist covers
      // anonymous trio events, and identified events flushed here may drop —
      // acceptable by the loss-tolerance contract at tab close.
      navigator.sendBeacon(
        endpoint(), new Blob([payload(events)], { type: 'application/json' }))
    }
  } catch { /* no-op */ }
}

try {
  if (!DISABLED && typeof addEventListener === 'function') {
    addEventListener('pagehide', beaconFlush)
  }
} catch { /* no-op */ }
