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
//
// ONE exception, added with the global error capture below: site/routeScene.js.
// It is a pure leaf module with zero imports of its own, so it cannot form a
// cycle, and it is the app's OWN route vocabulary. Re-deriving a route label
// here instead would drift the moment a route is added, and a hand-rolled one
// is exactly how a customer name reaches BigQuery inside a pathname.
import { sceneForPath } from './site/routeScene.js'
const DISABLED = import.meta.env?.VITE_TELEMETRY_DISABLED === '1'
const API_BASE = import.meta.env?.VITE_API_BASE ?? ''
const FLUSH_AT = 20
const FLUSH_MS = 5000
const BUFFER_MAX = 200          // absolute cap; beyond it the oldest drop
const ERROR_CAP = 20            // error.shown per session (design cap)
const STREAM_DOWN_CAP = 10      // agent.stream_down per session (design cap)
// Separate budgets ON PURPOSE: a global error storm (an animation callback
// throwing every frame) must not be able to spend the boundary's budget and
// suppress the one record of a real React crash.
const EXCEPTION_CAP = 10        // global-handler emissions per session
const BOUNDARY_CAP = 5          // ErrorBoundary crashes per session
const STACK_HEAD_MAX = 200

const state = {
  buffer: [],
  timer: null,
  sessionId: null,
  tourStep: null,
  globalsInstalled: false,
  seenExceptions: [],
}

/** Wave C-2: while the guided tour is active, every organic event carries a
 * `tour_step` label instead of duplicate tour.* variants of each event (the
 * tour rides the REAL handlers by design). Null clears it on exit. */
export function setTourStep(stepId) {
  try { state.tourStep = stepId == null ? null : String(stepId) } catch { /* no-op */ }
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
      labels: state.tourStep != null && props.tour_step === undefined
        ? { ...props, tour_step: state.tourStep }
        : props,
    })
    if (state.buffer.length > BUFFER_MAX) state.buffer.shift()
    if (state.buffer.length >= FLUSH_AT) flush()
    else schedule()
  } catch { /* telemetry never breaks the product */ }
}

/** Immediate flush for events that must land before an imminent navigation
 * (the post-auth reload): post() uses keepalive fetch WITH identity headers,
 * which survives the unload where the pagehide beacon cannot carry auth. */
export function flushNow() {
  try { flush() } catch { /* telemetry never breaks the product */ }
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

/** Crash capture — the ErrorBoundary AND the global handlers below. Capped
 * per session because a render loop or a retry loop can throw thousands of
 * times per minute, and an uncapped emitter would spend the ingest door's
 * whole token bucket on one broken page.
 *
 * `capKey` selects the budget. The boundary's default keeps its own, so a
 * storm of global errors can never consume the crash record. */
export function trackException(props = {}, capKey = 'exception_boundary') {
  try {
    const limit = capKey === 'exception_global' ? EXCEPTION_CAP : BOUNDARY_CAP
    if (capCount(capKey) >= limit) return
    capIncrement(capKey)
    track('client.exception', props, 'exception')
  } catch { /* no-op */ }
}

// --- global error capture (outside React) --------------------------------
// The ErrorBoundary only sees what a component throws DURING RENDER. A
// three.js draw tick, a setTimeout callback, an event handler, and every
// unawaited promise fail somewhere React never looks, so those failures were
// invisible: server EMF showed a healthy 200 while the page was dead.
//
// These emit the EXISTING `client.exception` event, distinguished by a
// `source` label — a new event name would need its own allowlist entry, its
// own dashboard row, and its own docs for no added meaning.

// NO FREE TEXT LEAVES THE BROWSER. An exception message is attacker- and
// user-influenced, and two independent adversarial reviews of this file
// demonstrated that a redactor cannot make it safe: a bare name in a path
// (`/app/alice/settings`), a Windows path, an IP literal, and an all-letter
// id (`deadbeef`) each survive every pattern that does not also destroy the
// ordinary diagnostics the label exists for. A denylist over free text is an
// arms race, and losing it once puts customer data in BigQuery permanently.
//
// So the message travels as a STABLE HASH, which is the convention this
// codebase already settled on for exactly this tension: ErrorBoundary emits
// `message_class` + `component_stack_hash` and no raw text. A hash still
// answers the questions that matter -- which distinct failure, how often,
// on which surface, at which line -- because it rides alongside `stack_head`
// (`fn@file:line:col`, which locates the code through a sourcemap) and
// `route`. What it costs is reading the text at a glance, and that is the
// right price for a guarantee instead of a best effort.
function hash32(text) {
  try {
    const s = String(text ?? '')
    let h = 0
    for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
    return String(h >>> 0)
  } catch { return '0' }
}

// Provenance, not syntax. `name` is an ordinary writable property, so an
// identifier-SHAPED check accepts `Promise.reject({name: 'AliceSmith'})` --
// a legal rejection any visitor can produce. Only the platform's own error
// names pass; everything else is `Other`, which is still a usable bucket.
const KNOWN_CLASSES = new Set([
  'Error', 'EvalError', 'RangeError', 'ReferenceError', 'SyntaxError',
  'TypeError', 'URIError', 'AggregateError', 'DOMException',
  // DOMException `name` values the platform assigns.
  'AbortError', 'ConstraintError', 'DataCloneError', 'DataError',
  'EncodingError', 'HierarchyRequestError', 'IndexSizeError',
  'InvalidCharacterError', 'InvalidStateError', 'NamespaceError',
  'NetworkError', 'NotAllowedError', 'NotFoundError', 'NotReadableError',
  'NotSupportedError', 'OperationError', 'QuotaExceededError',
  'ReadOnlyError', 'SecurityError', 'SyntaxError', 'TimeoutError',
  'TransactionInactiveError', 'UnknownError', 'VersionError',
  'WrongDocumentError',
])

const FRAME_FN_RE = /^[A-Za-z_$][A-Za-z0-9_$.<>]{0,63}$/
const FRAME_FILE_RE = /^[A-Za-z0-9_.-]{1,64}$/

function messageClass(err, fallback) {
  // Reading `.name` can itself throw: it may be a getter.
  try {
    const name = err && err.name
    if (typeof name === 'string' && KNOWN_CLASSES.has(name)) return name
  } catch { /* fall through */ }
  return fallback
}

/** The FIRST frame (the throw site) reduced to `fn@file:line:col`.
 *
 * BUILT FROM PARTS rather than redacted: the host, the directory path, the
 * query, and any data:/blob: payload are dropped by construction, so nothing
 * depends on a pattern anticipating them. What survives is the bundle's own
 * file name and position, which is the entire diagnostic value. */
function stackHead(err) {
  try {
    // Anchored: a message line like `Error: request failed at /x` contains
    // " at " and would otherwise be picked as the frame -- putting raw
    // message text into a label that is supposed to be structural.
    const lines = String((err && err.stack) || '').split('\n').map((l) => l.trim())
    const frame = lines.find((l) => /^at\s/.test(l) || /^[^@\s]*@\S/.test(l)) || ''
    if (!frame) return ''
    const v8 = frame.match(/^at\s+(?:(\S+)\s+)?\(?(.*?)\)?$/)
    const spider = v8 ? null : frame.match(/^([^@]*)@(.*)$/)
    const fnRaw = (v8 ? v8[1] : spider && spider[1]) || ''
    const locRaw = (v8 ? v8[2] : spider && spider[2]) || ''
    const fn = fnRaw ? (FRAME_FN_RE.test(fnRaw) ? fnRaw : '<fn>') : '<anon>'
    if (/^(?:data|blob|javascript):/i.test(locRaw)) return `${fn}@<inline>`
    const tail = locRaw.split(/[?#]/)[0].split('/').pop() || ''
    const at = tail.match(/^(.*?)(?::(\d+):(\d+))?$/) || []
    const file = FRAME_FILE_RE.test(at[1] || '') ? at[1] : '<file>'
    return `${fn}@${file}${at[2] ? `:${at[2]}:${at[3]}` : ''}`.slice(0, STACK_HEAD_MAX)
  } catch { return '' }
}

function routeLabel() {
  // The app's OWN route vocabulary (four static scene names), never the raw
  // pathname: `/app/Alice-Smith/invite/<code>` is a real shape this product
  // serves, and there is no redactor that reliably tells a customer name from
  // a route word. `sceneForPath` collapses every path by construction.
  try { return sceneForPath(String(location?.pathname || '/')) } catch { return 'unknown' }
}

function uaClass() {
  // A CLASS, never the raw user-agent string (which is a fingerprint).
  try {
    const ua = String(navigator?.userAgent || '')
    const family = /Edg\//.test(ua) ? 'edge'
      : /OPR\//.test(ua) ? 'opera'
        : /Firefox\//.test(ua) ? 'firefox'
          : /Chrome\//.test(ua) ? 'chrome'
            : /Safari\//.test(ua) ? 'safari'
              : 'other'
    return `${family}/${/Mobi|Android|iPhone|iPad/.test(ua) ? 'mobile' : 'desktop'}`
  } catch { return 'unknown' }
}

/** Emit one global exception, DE-DUPLICATED by signature.
 *
 * A repeat spends nothing. Without this, the cap counts occurrences rather
 * than distinct failures, and the two loud-and-benign classes this app
 * invites -- a ResizeObserver loop from the resizable panels, an animation
 * callback throwing every frame from the three.js viewer -- would spend all
 * ten slots before a genuinely different crash ever got one. The seen-set is
 * bounded by the cap itself, because past it nothing emits anyway. */
function emitGlobalException(props) {
  const sig = `${props.message_class}|${props.message_hash}|${props.stack_head}`
  if (state.seenExceptions.indexOf(sig) !== -1) return
  if (state.seenExceptions.length < EXCEPTION_CAP) state.seenExceptions.push(sig)
  trackException(props, 'exception_global')
}

/** `error` listener. Exported so a spec can drive it without synthesizing a
 * browser event jsdom does not fully implement. */
export function handleErrorEvent(ev) {
  try {
    const err = ev && ev.error
    // Resource-load failures (a 404 <img>, a blocked script tag) dispatch a
    // plain Event with neither `error` nor `message`. They are not JS
    // exceptions, they arrive in bursts, and they would spend the cap.
    if (!err && !(ev && ev.message)) return
    emitGlobalException({
      source: 'window.onerror',
      message_class: messageClass(err, 'Other'),
      message_hash: hash32((err && err.message) || (ev && ev.message)),
      stack_head: stackHead(err),
      route: routeLabel(),
      ua_class: uaClass(),
    })
  } catch { /* telemetry never breaks the product */ }
}

/** `unhandledrejection` listener. */
export function handleRejectionEvent(ev) {
  try {
    const reason = ev && ev.reason
    // A rejection value need not be an Error: `Promise.reject('nope')` and
    // `reject({code: 4})` are both legal and both worth seeing.
    const fallback = reason instanceof Error ? 'Error' : 'UnhandledRejection'
    const text = reason && typeof reason === 'object' && 'message' in reason
      ? reason.message
      : reason
    emitGlobalException({
      source: 'unhandledrejection',
      message_class: messageClass(reason, fallback),
      message_hash: hash32(text),
      stack_head: stackHead(reason),
      route: routeLabel(),
      ua_class: uaClass(),
    })
  } catch { /* telemetry never breaks the product */ }
}

/** Idempotent; returns whether it installed. Uses addEventListener rather
 * than `window.onerror = fn` for the reason the pagehide listener does:
 * assignment clobbers whatever else the page installed, and is itself
 * clobbered by the next script that assigns. */
export function installGlobalErrorHandlers() {
  try {
    if (DISABLED || state.globalsInstalled) return false
    if (typeof addEventListener !== 'function') return false
    addEventListener('error', handleErrorEvent)
    addEventListener('unhandledrejection', handleRejectionEvent)
    state.globalsInstalled = true
    return true
  } catch { return false }
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
    installGlobalErrorHandlers()
  }
} catch { /* no-op */ }
