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
// The cap belongs to the NEW path only. An animation callback throwing every
// frame is a storm the browser sustains indefinitely; a React crash is not,
// because the boundary replaces the tree with a reload card. See trackException.
const EXCEPTION_CAP = 10        // global-handler emissions per session
const HASH_INPUT_MAX = 4096     // bound the work a digest does on the main thread
const KEY_SAMPLE_MAX = 20       // keys sampled when describing an unserializable reason
const DIGEST_WIDTH = 16         // 2^53-1 is 16 digits; a digest is ALWAYS this wide
const CROSS_PATH_MS = 10000     // how long a released global row stays retractable
// How long a global row waits OUTSIDE the send buffer for its boundary twin.
// It only has to outlast the ErrorBoundary's dynamic import, which is one
// microtask when the chunk is already in the graph and one fetch when it is
// not. A second covers both and still lands inside the buffer's own 5 s
// latency, so nothing is reported later than it would have been anyway.
const PENDING_MS = 1000

const state = {
  buffer: [],
  timer: null,
  sessionId: null,
  tourStep: null,
  globalsInstalled: false,
  seenExceptions: [],
  globalRows: [],
  pendingGlobals: [],
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

/** Give a slot back. Only the cross-path retraction below uses it: a row that
 * is pulled out of the buffer before it is ever sent must not spend the budget
 * that exists to bound what the DOOR receives. */
function capDecrement(key) {
  capSet(key, Math.max(0, capCount(key) - 1))
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

/** Build one event WITHOUT queuing it. Split out for the global exception
 * path below, which has to own a row for a moment before deciding whether it
 * is ever sent; every other caller goes through `track`. */
function buildEvent(name, props, eventType) {
  if (DISABLED || typeof fetch !== 'function') return undefined
  return {
    event_type: eventType,
    event_name: name,
    client_ts: Date.now() / 1000,
    labels: state.tourStep != null && props.tour_step === undefined
      ? { ...props, tour_step: state.tourStep }
      : props,
  }
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
 * threw. The only caller that reads it is the global exception path below,
 * which must be able to identify ITS OWN row later to withdraw it; identity is
 * the only handle that survives a buffer that shifts and splices. */
export function track(name, props = {}, eventType = 'custom_event') {
  try {
    const event = buildEvent(name, props, eventType)
    if (!event) return undefined
    enqueue(event)
    return event
  } catch { return undefined /* telemetry never breaks the product */ }
}

/** Immediate flush for events that must land before an imminent navigation
 * (the post-auth reload): post() uses keepalive fetch WITH identity headers,
 * which survives the unload where the pagehide beacon cannot carry auth.
 *
 * Releases held rows first: a row waiting for a boundary twin that is about
 * to be navigated away from has run out of window, and the record is worth
 * more than the de-duplication. */
export function flushNow() {
  try {
    releaseAllPending()
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
 * `error` is the Error the boundary caught. Passing it lets this path withdraw
 * the global row for the SAME failure, so one React crash is one row.
 *
 * `hold` builds the row and returns it UNQUEUED, for the global path's
 * pending window below. It stays a parameter of this function rather than a
 * shortcut around it because the label filter under it is a choke point: a
 * caller that builds its own row is a caller that can put free text in a
 * label again. */
export function trackException(props = {}, capKey = 'exception_boundary', error = undefined, hold = false) {
  try {
    if (capKey === 'exception_global') {
      if (capCount(capKey) >= EXCEPTION_CAP) return undefined
      capIncrement(capKey)
    } else if (error != null) {
      // Held first, buffered second: one failure has at most one global row,
      // and it lives in exactly one of the two places.
      const sig = crossPathSignature(error)
      if (!dropPendingTwin(sig)) retractGlobalTwin(sig)
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
    if (hold) return buildEvent('client.exception', safe, 'exception')
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
    // shift-hash collided on inputs as short as "Aa"/"BB", which silently
    // suppressed the second of two distinct failures through the dedup below.
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
 * collapse to one signature or the dedup below silently drops the second.
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

// --- one React crash is one row --------------------------------------------
// React 18's DEVELOPMENT build re-throws a render error through a synthetic
// DOM event so DevTools can observe it, so ONE crash reaches BOTH emitters:
// the global handler first (synchronously, during render) and the boundary
// second (behind its dynamic import). Its production build uses try/catch and
// need not. That made the row count a property of the BUILD, and anything
// counting crashes counted them differently depending on which one it watched.
//
// The BOUNDARY wins the tie. Its row carries `component_stack_hash`, which the
// global handler cannot know, and it is the older contract.
//
// The global handler stays synchronous -- it observes and records the failure
// the moment it happens -- but its row is HELD OUT of the send buffer for a
// short window instead of being queued into it. A held row cannot be flushed,
// so the boundary can still drop it however many events happen to be queued
// behind it.
//
// This replaces retracting an already-buffered row, which worked only while
// the row was still buffered: at 19 queued events the global row was the 20th,
// the batch POSTed on the spot, and the boundary a microtask later had nothing
// left to pull back. Two rows landed, and whether they did was decided by how
// busy the page was -- the guarantee held on a quiet page and broke on a busy
// one. Retraction is KEPT below as the fallback for a boundary that arrives
// after the hold expires but before the batch goes out.
//
// Nothing is traded away for it. The reason the original chose retraction was
// that deferring an emit loses the record if the page is torn down first, so
// every exit seam -- the pagehide beacon and the pre-navigation flushNow --
// releases held rows before it sends, and the hold expires on its own timer
// for the ordinary case where no boundary is ever coming.

/** The cross-path identity of ONE failure: everything BOTH emitters can derive
 * from the same Error object, and nothing else. `source` is deliberately
 * absent -- it is the one label that must differ between the two paths for a
 * single crash. */
function crossPathSignature(err) {
  let message = ''
  try { message = (err && err.message) || '' } catch { /* a getter threw */ }
  return [messageClass(err, 'Other'), digest(message), stackHash(err), routeLabel()].join('|')
}

function noteGlobalRow(sig, event) {
  try {
    if (!sig || !event) return
    const now = Date.now()
    state.globalRows = state.globalRows.filter((r) => now - r.at < CROSS_PATH_MS)
    state.globalRows.push({ sig, event, at: now })
    while (state.globalRows.length > EXCEPTION_CAP) state.globalRows.shift()
  } catch { /* no-op */ }
}

/** Hold one global row OUT of the send buffer for the dedup window.
 *
 * A held row is unreachable from `flush`, so no batch threshold and no timer
 * can put it on the wire while a boundary might still claim it. */
function holdGlobalRow(sig, event) {
  try {
    if (!event) return
    // No signature is no dedup handle, so holding it could only lose it.
    if (!sig) { enqueue(event); return }
    const row = { sig, event, timer: null }
    state.pendingGlobals.push(row)
    // Bounded by the global cap on the way in, but held explicitly too: a
    // pending list that could grow is a leak, and this one holds crash rows.
    while (state.pendingGlobals.length > EXCEPTION_CAP) releasePendingRow(state.pendingGlobals[0])
    try {
      row.timer = setTimeout(() => releasePendingRow(row), PENDING_MS)
    } catch {
      // No timer available: send it now rather than hold it forever. The old
      // behaviour, which is still correct, just weaker.
      releasePendingRow(row)
    }
  } catch { /* no-op */ }
}

/** The hold is over: the row joins the send buffer and becomes an ordinary
 * queued event -- retractable while it is still buffered, and nothing more. */
function releasePendingRow(row) {
  try {
    const at = state.pendingGlobals.indexOf(row)
    if (at === -1) return
    state.pendingGlobals.splice(at, 1)
    if (row.timer) { clearTimeout(row.timer); row.timer = null }
    enqueue(row.event)
    noteGlobalRow(row.sig, row.event)
  } catch { /* no-op */ }
}

/** Every exit seam calls this: a record is worth more than a de-duplication
 * the page will not be alive to perform. */
function releaseAllPending() {
  try {
    while (state.pendingGlobals.length) releasePendingRow(state.pendingGlobals[0])
  } catch { /* no-op */ }
}

/** Drop the held twin of `sig` before it is ever queued. Returns whether one
 * was actually held -- the caller falls back to buffer retraction when not. */
function dropPendingTwin(sig) {
  try {
    if (!sig) return false
    for (let i = state.pendingGlobals.length - 1; i >= 0; i--) {
      const row = state.pendingGlobals[i]
      if (row.sig !== sig) continue
      state.pendingGlobals.splice(i, 1)
      if (row.timer) clearTimeout(row.timer)
      // Same refund as a retraction: a row nobody ever received must not spend
      // the budget that exists to bound what the DOOR receives.
      capDecrement('exception_global')
      return true
    }
    return false
  } catch { return false }
}

/** Pull the global twin of `sig` back out of the send buffer. Returns whether
 * a row was actually withdrawn (false once it is already on the wire). */
function retractGlobalTwin(sig) {
  try {
    if (!sig) return false
    const now = Date.now()
    for (let i = state.globalRows.length - 1; i >= 0; i--) {
      const row = state.globalRows[i]
      if (now - row.at >= CROSS_PATH_MS) { state.globalRows.splice(i, 1); continue }
      if (row.sig !== sig) continue
      state.globalRows.splice(i, 1)
      const at = state.buffer.indexOf(row.event)
      if (at === -1) return false
      state.buffer.splice(at, 1)
      capDecrement('exception_global')
      return true
    }
    return false
  } catch { return false }
}

/** Emit one global exception, DE-DUPLICATED by signature.
 *
 * A repeat spends nothing. Without this, the cap counts occurrences rather
 * than distinct failures, and the two loud-and-benign classes this app
 * invites -- a ResizeObserver loop from the resizable panels, an animation
 * callback throwing every frame from the three.js viewer -- would spend all
 * ten slots before a genuinely different crash ever got one. The seen-set is
 * bounded by the cap itself, because past it nothing emits anyway. */
function emitGlobalException(props, err) {
  const sig = [props.source, props.message_class, props.message_hash,
    props.stack_hash, props.route].join('|')
  if (state.seenExceptions.indexOf(sig) !== -1) return
  if (state.seenExceptions.length < EXCEPTION_CAP) state.seenExceptions.push(sig)
  // Only `window.onerror` rows can have a boundary twin: a rejection never
  // reaches a React boundary, so holding one could only ever delay a row for a
  // match that cannot come.
  const twinnable = err != null && props.source === 'window.onerror'
  const event = trackException(props, 'exception_global', undefined, twinnable)
  if (event && twinnable) holdGlobalRow(crossPathSignature(err), event)
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
    }, err)
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
    }, reason)
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
    // The tab is going away, so every held row has run out of window. This is
    // the seam that makes holding safe: a crash recorded a moment before the
    // page was torn down still leaves with the beacon.
    releaseAllPending()
    if (!state.buffer.length) return
    const events = state.buffer.splice(0, FLUSH_AT)
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
