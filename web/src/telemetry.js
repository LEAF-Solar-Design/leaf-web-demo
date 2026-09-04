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
// The consent rail (slice 13c). A second leaf module — pure, no imports of
// its own beyond the platform — so it cannot form a cycle either. The rule it
// carries is documented at buildEvent: usage-shaped events need the viewer's
// yes, product events do not.
import { subscribeUsageConsent, usageConsentGranted } from './lib/telemetryConsent.js'
const DISABLED = import.meta.env?.VITE_TELEMETRY_DISABLED === '1'

/** The build-time kill switch, exported so the Plan panel can state the
 * honest reason its consent row is disabled instead of re-reading the env
 * (two readers of one fence is how they disagree). */
export const TELEMETRY_BUILD_DISABLED = DISABLED

/** The two classes of event this module carries. See buildEvent. */
export const EVENT_CLASS = Object.freeze({
  // What the app did: a run finished, a version restored, an exception was
  // caught. The operational record. Gated only by the build-time kill switch,
  // exactly as before slice 13c.
  product: 'product',
  // What a person typed or picked: search queries, menu actions, palette
  // picks. Describes the viewer, so it needs the viewer's yes.
  usage: 'usage',
})

// Every queued event carries its class, because a revoke has to find events
// that are ALREADY in the buffer. The key is a SYMBOL on purpose:
// JSON.stringify skips symbol-keyed properties by construction, so this
// internal label can never reach the ingest door however `payload()` is later
// changed. An own string key would need a strip step a future edit can forget.
const EVENT_CLASS_KEY = Symbol('leaf.telemetry.eventClass')
const API_BASE = import.meta.env?.VITE_API_BASE ?? ''
const FLUSH_AT = 20
const FLUSH_MS = 5000
const BUFFER_MAX = 200          // absolute cap; beyond it the oldest drop
const ERROR_CAP = 20            // error.shown per session (design cap)
const STREAM_DOWN_CAP = 10      // agent.stream_down per session (design cap)
// The cap belongs to the NEW path only. An animation callback throwing every
// frame is a storm the browser sustains indefinitely; a React crash is not,
// because the boundary replaces the tree with a reload card. See trackException.
const EXCEPTION_CAP = 10        // global-handler emissions per session
const HASH_INPUT_MAX = 4096     // bound the work a digest does on the main thread
const KEY_SAMPLE_MAX = 20       // keys sampled when describing an unserializable reason
const DIGEST_WIDTH = 16         // 2^53-1 is 16 digits; a digest is ALWAYS this wide
// Batches waiting on the 2 s post-failure retry. Bounded for the same reason
// BUFFER_MAX exists: a host that rejects every POST must not be able to grow
// an unbounded set of pending arrays. Past the cap a batch is DROPPED, which
// telemetry is allowed to do (loss-tolerant by contract) and a leak is not.
const RETRY_BATCH_MAX = 8
const state = {
  buffer: [],
  // The batches the retry timer is holding. They live OUT of `buffer`, so a
  // revoke that only compacted `buffer` would miss them: see purgeUsageEvents.
  retryBatches: new Set(),
  timer: null,
  sessionId: null,
  tourStep: null,
  globalsInstalled: false,
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
  capSet(key, capCount(key) + 1)
}

function capSet(key, next) {
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

// Background bookkeeping only — this buffer must never be the reason a
// process (a Node test runner, a script host embedding this module) stays
// alive. Browsers have no ref/unref concept and setTimeout there returns a
// number, so `unref` is Node-only and guarded accordingly.
function unrefTimer(timer) {
  try { timer?.unref?.() } catch { /* no-op */ }
}

/** The WIRE-TIME consent fence, and the second half of the guarantee
 * `buildEvent` starts. buildEvent decides at BUILD time; this decides at SEND
 * time, which is the only moment that actually matters to a viewer who
 * revoked while events were sitting behind the 5 s timer.
 *
 * Returns the batch UNCHANGED (no copy, no allocation) while consent is
 * granted — the only path a consenting viewer ever takes. Only a revoked
 * viewer pays one O(batch <= FLUSH_AT) pass. Fails closed: an event carrying
 * no class marker is treated as usage-shaped. */
function sendable(events) {
  if (usageConsentGranted()) return events
  return events.filter(isProduct)
}

function isProduct(ev) {
  return ev?.[EVENT_CLASS_KEY] === EVENT_CLASS.product
}

/** Drop every usage-class event out of ONE queue, IN PLACE. The array
 * identity is load-bearing (a caller may be a splice already in flight, or a
 * retry closure that captured the array), so this never rebinds or copies.
 * O(n) over a queue already bounded by BUFFER_MAX or FLUSH_AT. */
function compactToProduct(events) {
  let kept = 0
  for (let i = 0; i < events.length; i += 1) {
    const ev = events[i]
    if (isProduct(ev)) { events[kept] = ev; kept += 1 }
  }
  events.length = kept
}

/** Drop every usage-class event still waiting ANYWHERE in this module. Runs
 * ONLY on a revoke, so the emit path stays allocation-free.
 *
 * BOTH queues, and the second one is the whole point: `state.buffer` holds
 * what has not been sent yet, and `state.retryBatches` holds the batches a
 * failed POST handed to a 2 s timer. A purge of the buffer alone leaves the
 * retry as the same leak on a longer fuse: revoke, re-grant inside that
 * window, and the send-time fence sees consent again and posts what the viewer
 * revoked. Destroying the events here is what makes a revoke DESTRUCTIVE
 * rather than merely fenced. */
function purgeUsageEvents() {
  try {
    compactToProduct(state.buffer)
    for (const batch of state.retryBatches) compactToProduct(batch)
  } catch { /* telemetry never breaks the product */ }
}

// A revoke must reach events ALREADY QUEUED, not only the next one built.
// Without this subscription, up to FLUSH_AT usage events sitting behind the
// 5 s timer (or the pagehide beacon, or the 2 s post-failure retry) would
// still leave the browser after the viewer said no.
try {
  subscribeUsageConsent((granted) => { if (!granted) purgeUsageEvents() })
} catch { /* no-op: sendable() still fences every send seam */ }

function flush() {
  try {
    if (state.timer) { clearTimeout(state.timer); state.timer = null }
    if (!state.buffer.length) return
    const events = sendable(state.buffer.splice(0, FLUSH_AT))
    if (!events.length) { if (state.buffer.length) schedule(); return }
    // The batch is REGISTERED BEFORE the request leaves, not in the failure
    // callback: it is no longer in `state.buffer`, and a revoke that lands
    // while the POST is still on the wire must reach it too, or a failure
    // after that revoke would arm a retry carrying the very events the
    // viewer took back. payload() serialized the body at call time, so a
    // revoke compacting this array in place cannot touch the request in
    // flight; it only decides what the ONE retry may carry. Bounded: past
    // RETRY_BATCH_MAX a batch is sent once and never retried. sendable()
    // below still re-fences, for a revoke that raced the subscription (a
    // listener set at LISTENER_MAX refuses new subscribers).
    const tracked = state.retryBatches.size < RETRY_BATCH_MAX
    if (tracked) state.retryBatches.add(events)
    post(events).then(() => {
      if (tracked) state.retryBatches.delete(events)
    }, () => {
      // ONE retry PER BATCH, then drop (loss-tolerant by contract).
      if (!tracked) return
      unrefTimer(setTimeout(() => {
        state.retryBatches.delete(events)
        const retry = sendable(events)
        if (retry.length) post(retry).catch(() => {})
      }, 2000))
    })
    if (state.buffer.length) schedule()
  } catch { /* telemetry never breaks the product */ }
}

function schedule() {
  try {
    if (state.timer) return
    state.timer = setTimeout(flush, FLUSH_MS)
    unrefTimer(state.timer)
  } catch { /* no-op */ }
}

/** Build one event WITHOUT queuing it. Kept split from `track` because the
 * DISABLED / no-fetch / CONSENT decision belongs in exactly one place.
 *
 * `eventClass` is REQUIRED and FAILS CLOSED: only the literal
 * `EVENT_CLASS.product` is a product event. An omitted class, a typo, a class
 * a future caller invents — every one of them is treated as usage-shaped and
 * therefore needs consent. The safe default has to be the restrictive one,
 * because the failure mode of the other default is collecting a person's
 * search queries without asking, and that is not recoverable by a later fix.
 *
 * Exported for its own spec: the refusal is the product promise ("no
 * usage-shaped data before the toggle is on"), so it is tested at the seam
 * where it is decided, not inferred from a call site.
 *
 * A refused event is not built, so `track` cannot queue it: there is no
 * buffered usage event waiting for a later consent to release it.
 *
 * The OTHER direction is deliberately NOT this function's job and is not
 * implied by it: an event built while consent was granted can still be in the
 * buffer when the viewer revokes a second later. That half is carried by
 * `purgeUsageEvents` (the buffer is emptied of usage events on revoke) and by
 * `sendable` (every send seam re-checks). This function only stamps the class
 * those two read. */
export function buildEvent(name, props, eventType, eventClass) {
  if (DISABLED || typeof fetch !== 'function') return undefined
  if (eventClass !== EVENT_CLASS.product && !usageConsentGranted()) return undefined
  const event = {
    event_type: eventType,
    event_name: name,
    client_ts: Date.now() / 1000,
    labels: state.tourStep != null && props.tour_step === undefined
      ? { ...props, tour_step: state.tourStep }
      : props,
  }
  // Stamp the class so a later revoke can find this event again once it is
  // sitting in the shared buffer. Fails closed exactly as the gate above does:
  // anything but the exact product literal is marked usage-shaped.
  event[EVENT_CLASS_KEY] =
    eventClass === EVENT_CLASS.product ? EVENT_CLASS.product : EVENT_CLASS.usage
  return event
}

function enqueue(event) {
  state.buffer.push(event)
  if (state.buffer.length > BUFFER_MAX) state.buffer.shift()
  if (state.buffer.length >= FLUSH_AT) flush()
  else schedule()
}

/** Queue one event. `props` values should be primitives; the server
 * stringifies and bounds everything and strips reserved keys.
 *
 * Returns the queued event, or undefined when telemetry is off or the queue
 * threw. */
export function track(name, props = {}, eventType = 'custom_event') {
  try {
    const event = buildEvent(name, props, eventType, EVENT_CLASS.product)
    if (!event) return undefined
    enqueue(event)
    return event
  } catch { return undefined /* telemetry never breaks the product */ }
}

/** Queue one USAGE-SHAPED event — a search query, a menu action, a palette
 * pick: anything that describes how a person uses the studio rather than what
 * the app did.
 *
 * Returns undefined and queues NOTHING when the viewer has not turned the Plan
 * panel's "Usage telemetry" switch on. There is no buffering-until-consent and
 * no retroactive send: a refused event does not exist.
 *
 * Slices 10-13's emitters call THIS, never `track`. The separation is the
 * whole guarantee — one function whose events are gated, one whose events are
 * the operational record — and a caller that picks the wrong one is a review
 * finding, not a runtime surprise, because the names say which is which. */
export function trackUsage(name, props = {}, eventType = 'custom_event') {
  try {
    const event = buildEvent(name, props, eventType, EVENT_CLASS.usage)
    if (!event) return undefined
    enqueue(event)
    return event
  } catch { return undefined /* telemetry never breaks the product */ }
}

/** Immediate flush for events that must land before an imminent navigation
 * (the post-auth reload): post() uses keepalive fetch WITH identity headers,
 * which survives the unload where the pagehide beacon cannot carry auth. */
export function flushNow() {
  try {
    flush()
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

/** Crash capture — the ErrorBoundary AND the global handlers below.
 *
 * ONLY the global path is capped. A stray callback can throw thousands of
 * times per minute with nothing to stop it, so an uncapped global emitter
 * would spend the ingest door's whole token bucket on one broken page.
 *
 * The BOUNDARY is uncapped, exactly as it was before global capture existed.
 * Capping it looked like symmetry and was a regression: the boundary fires
 * once per crash and the card it renders invites a reload, so five
 * crash-and-reload cycles in one browser session would have silenced every
 * later PRODUCTION crash on that tab -- and the crash a user hits after
 * already hitting several is the one most worth seeing. React bounds this
 * emitter's volume; we do not have to.
 *
 * NO CROSS-EMITTER DE-DUPLICATION. The global handler and the boundary each
 * emit independently, with no shared key. See the comment above
 * `emitGlobalException` for why one cannot be derived reliably on the client. */
export function trackException(props = {}, capKey = 'exception_boundary') {
  try {
    if (capKey === 'exception_global') {
      if (capCount(capKey) >= EXCEPTION_CAP) return undefined
      capIncrement(capKey)
    }
    // The class is filtered HERE, not at each call site, so every emitter of
    // this event is covered by construction. ErrorBoundary passed
    // `error.name` verbatim, which meant a component that render-threw an
    // error named `owner@example.com` put that text in a label -- under the
    // same event whose contract promises structural labels. One choke point
    // is also the only way a future caller cannot reintroduce it.
    const safe = props && props.message_class !== undefined
      ? { ...props, message_class: KNOWN_CLASSES.has(props.message_class) ? props.message_class : 'Other' }
      : props
    return track('client.exception', safe, 'exception')
  } catch { return undefined /* no-op */ }
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
// `message_class` + `component_stack_hash` and no raw text. Paired with
// `stack_hash` and `route`, a digest still answers which distinct failure,
// how often, on which surface. What it costs is reading the text at a
// glance, and that is the right price for a guarantee instead of a best
// effort.
//
// A digest of a string that MAY have contained personal data is pseudonymous,
// not anonymous: anyone with BigQuery access and a candidate list can hash
// the candidates and compare. That is a real and deliberate limit. It is
// still strictly better than the text, and BigQuery access is already
// privileged and already sees the row's stamped tenant.
export function digest(text) {
  try {
    // cyrb53: two accumulators combined into ~53 bits. The previous 32-bit
    // shift-hash collided on inputs as short as "Aa"/"BB", which made two
    // distinct failures share a hash and become indistinguishable in the
    // stored labels.
    //
    // Input is BOUNDED before hashing. The redactor this replaced happened to
    // cap its input; deleting it removed that protection, so a server-supplied
    // multi-megabyte message would hash synchronously on the main thread.
    const s = String(text ?? '').slice(0, HASH_INPUT_MAX)
    let h1 = 0xdeadbeef
    let h2 = 0x41c6ce57
    for (let i = 0; i < s.length; i++) {
      const ch = s.charCodeAt(i)
      h1 = Math.imul(h1 ^ ch, 2654435761)
      h2 = Math.imul(h2 ^ ch, 1597334677)
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909)
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909)
    // FIXED WIDTH, always 16 digits. The door cannot prove a digest's
    // provenance -- an opaque field holds whatever the caller puts in it --
    // but it CAN require the exact width of one, so the field's capacity is a
    // digest and nothing else. A variable-width decimal accepted `5550142`,
    // which is a phone number, not a hash.
    return String(4294967296 * (2097151 & h2) + (h1 >>> 0)).padStart(DIGEST_WIDTH, '0')
  } catch { return ''.padStart(DIGEST_WIDTH, '0') }
}

/** A rejection reason need not be an Error, and two different ones must not
 * collapse to one hash, or they become indistinguishable in the stored labels.
 *
 * Four descents, each bounded and none able to throw out of here: the
 * message, a JSON serialization, a per-key value description (BigInt and
 * Symbol values make `JSON.stringify` throw or omit, so `{code: 1n}` and
 * `{code: 2n}` both reached "[object Object]"), and a constant. `String()` is
 * never the last resort, because a hostile `Symbol.toPrimitive` makes IT
 * throw too, which lost the whole record rather than degrading it. */
function reasonText(reason) {
  if (reason === null || typeof reason !== 'object') {
    try { return String(reason) } catch { return '[unserializable]' }
  }
  try { if ('message' in reason) return reason.message } catch { /* next */ }
  try {
    // Bounds the NODE COUNT, not the byte count: `JSON.stringify` walks the
    // whole graph and runs every getter and `toJSON` it meets, and the
    // replacer aborts that walk once it has seen enough to tell one failure
    // from another. A single enormous string VALUE still materializes in one
    // replacer call before `digest` slices it. That residue is self-inflicted
    // -- only code already running in the page can reject with it, and such
    // code can stall the main thread far more cheaply -- and nothing
    // oversized is ever stored, because the digest is fixed width.
    let budget = HASH_INPUT_MAX
    const json = JSON.stringify(reason, (k, v) => {
      budget -= String(k).length + 8
      if (budget < 0) throw new RangeError('bounded')
      return v
    })
    if (typeof json === 'string') return json
  } catch { /* next */ }
  try {
    // `Object.keys` materializes EVERY key (and runs a proxy's ownKeys trap)
    // before any slice could bound it; for-in with a break never does.
    const parts = []
    for (const k in reason) {
      if (parts.length >= KEY_SAMPLE_MAX) break
      try { parts.push(`${k}:${String(reason[k])}`) } catch { parts.push(`${k}:?`) }
    }
    return parts.sort().join(',')
  } catch { /* next */ }
  return '[unserializable]'
}

// Provenance, not syntax. `name` is an ordinary writable property, so an
// identifier-SHAPED check accepts `Promise.reject({name: 'AliceSmith'})` --
// a legal rejection any visitor can produce. Only the platform's own error
// names pass; everything else is `Other`, which is still a usable bucket.
const KNOWN_CLASSES = new Set([
  'Error', 'EvalError', 'RangeError', 'ReferenceError', 'SyntaxError',
  'TypeError', 'URIError', 'AggregateError', 'DOMException',
  // Values THIS module assigns when the platform supplies none. They must be
  // listed, or the choke-point filter in trackException degrades the very
  // fallbacks the handlers just computed.
  'UnhandledRejection', 'Other',
  // This app's own class (web/src/fetchBudget.js); its provenance is ours.
  'FetchTimeoutError',
  // Bundler-assigned, and this app CODE-SPLITS -- ErrorBoundary itself uses a
  // dynamic import -- so a tab left open across a deploy throwing
  // ChunkLoadError is an ordinary, foreseeable failure. Degrading it to
  // `Other` would lose the one class most worth seeing after a release.
  'ChunkLoadError',
  // DOMException `name` values the platform assigns.
  'AbortError', 'ConstraintError', 'DataCloneError', 'DataError',
  'EncodingError', 'HierarchyRequestError', 'IndexSizeError',
  'InvalidCharacterError', 'InvalidStateError', 'NamespaceError',
  'NetworkError', 'NotAllowedError', 'NotFoundError', 'NotReadableError',
  'NotSupportedError', 'OperationError', 'QuotaExceededError',
  'ReadOnlyError', 'SecurityError', 'TimeoutError',
  'TransactionInactiveError', 'UnknownError', 'VersionError',
  'WrongDocumentError',
])

function messageClass(err, fallback) {
  // Reading `.name` can itself throw: it may be a getter.
  try {
    const name = err && err.name
    if (typeof name === 'string' && KNOWN_CLASSES.has(name)) return name
  } catch { /* fall through */ }
  return fallback
}

/** A digest of the FIRST frame -- the throw site -- never its text.
 *
 * This was `fn@file:line:col`, built from parts, and it was the one label
 * that still exported caller-controlled text: `FRAME_FN_RE` and
 * `FRAME_FILE_RE` checked SHAPE, not provenance, so a crafted
 * `Promise.reject({stack: 'at AliceSmith (index.js:1:2)'})` put `AliceSmith`
 * straight into a label. That is the same defect the class allowlist fixes,
 * and keeping it made the docs' "every label is structural" claim FALSE --
 * worse than an honest best-effort, because consumers build on the claim.
 *
 * So the frame is grouped, not read. Paired with `message_hash`, `route` and
 * a count, it answers which distinct throw site, how often, on which surface.
 * Binding a hash to a line takes one reproduction, which is exactly how this
 * codebase's ErrorBoundary has always used `component_stack_hash`. */
function stackHash(err) {
  try {
    // Bounded BEFORE any parsing: the first frame is always within the first
    // few lines, so a multi-megabyte stack costs nothing extra. Splitting and
    // mapping the whole stack to find one line was both the redundant work
    // and the unbounded one.
    const stack = String((err && err.stack) || '').slice(0, HASH_INPUT_MAX)
    const frame = /^[ \t]*(at .+|[^@\s]*@\S+)$/m.exec(stack)
    return frame ? digest(frame[1].trim()) : ''
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

// --- no cross-emitter de-duplication (documented, deliberate) --------------
// Four review rounds tried to make one browser crash land as exactly one
// `client.exception` row, by giving the global handler and the ErrorBoundary
// a shared occurrence key: three rounds of client-side timing (emit-then-
// retract, then hold-then-release-on-flush-or-pagehide) each made the row
// count depend on how busy the page was, and a fourth (memoize a key on the
// Error object itself, WeakMap-keyed) turned out to depend on an assumption
// that does not hold.
//
// THE ASSUMPTION: that DEV React's re-throw of a render error through a
// synthetic DOM event hands `window.onerror` and `componentDidCatch` the SAME
// Error object instance. A probe against this exact ErrorBoundary (a real
// component, a real render failure, no mocking) showed otherwise: DEV React
// calls the throwing render function more than once for one logical crash,
// so `window.onerror` and `componentDidCatch` routinely observe two DIFFERENT
// Error instances with identical message/class/stack. Identity survives only
// when the render function throws a single reused reference (e.g. a
// module-scoped Error thrown by value, never reconstructed) -- not how a real
// crash is written; a plain `throw new Error(...)`, or a native TypeError the
// engine builds fresh each time the offending expression runs, constructs a
// NEW object on every invocation. With no reliable identity, the WeakMap
// fell back to matching on message+class+stack+route within a coarse time
// window, which silently merged two genuinely DISTINCT same-signature crashes
// into one row -- a worse defect than the duplicate it existed to remove.
//
// So there is no client occurrence key, and the two emitters below do not
// coordinate at all: each simply calls `trackException` when it sees a
// failure.
//
// PRODUCTION IMPACT IS BOUNDED: React's production build uses a plain
// try/catch, not the synthetic-event re-throw, so a boundary-caught crash in
// production never also reaches `window.onerror` -- there is no
// production-build double-count. The double-emit this leaves unresolved is a
// DEVELOPMENT-BUILD-ONLY cosmetic (an inflated crash count while dev-serving
// or running tests locally). Documented follow-up, not solved here: PR #537.

/** Emit one global exception. No same-signature suppression either: a storm
 * of identical errors (a ResizeObserver loop from the resizable panels, a
 * three.js draw-tick throwing every frame) simply spends the EXCEPTION_CAP
 * slots like any other occurrence, in exchange for the simplicity above. */
function emitGlobalException(props) {
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
      message_hash: digest((err && err.message) || (ev && ev.message)),
      stack_hash: stackHash(err),
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
    emitGlobalException({
      source: 'unhandledrejection',
      message_class: messageClass(reason, fallback),
      message_hash: digest(reasonText(reason)),
      stack_hash: stackHash(reason),
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
    if (DISABLED) return
    // Nothing is held back any more, so this seam has nothing to release: a
    // crash recorded a moment before the page was torn down is already in the
    // buffer and leaves with the beacon like any other event.
    if (!state.buffer.length) return
    // The same wire-time fence flush() uses. pagehide is the seam a revoke is
    // MOST likely to race, because turning the switch off and then closing the
    // tab is one continuous human action.
    const events = sendable(state.buffer.splice(0, FLUSH_AT))
    if (!events.length) return
    if (navigator?.sendBeacon) {
      // sendBeacon cannot carry auth headers; the pre-auth allowlist covers
      // the anonymous allowlist, and identified events flushed here may drop —
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
