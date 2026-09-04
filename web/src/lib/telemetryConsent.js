// The ONE source of truth for usage-telemetry consent (slice 13c).
//
// Two classes of telemetry event exist in this product:
//
//   * PRODUCT events — what the app did (a run finished, a version restored,
//     an exception was caught). They are the operational record, they carry no
//     description of how a person browses, and their only gate is the
//     build-time kill switch VITE_TELEMETRY_DISABLED=1. Behaviour unchanged.
//
//   * USAGE-SHAPED events — what a person typed and picked (search queries,
//     menu actions, palette picks; slices 10-13 will emit these). They
//     describe the viewer, not the system, so they need the viewer's yes.
//     NOTHING usage-shaped leaves the browser before this module says granted.
//
// The panel (EntitlementGate.jsx) and the emitter (telemetry.js) both read
// HERE. Two copies of a consent rule is how one of them drifts and collects
// what the other promised it would not.
//
// HARDENING CONTRACT, all four clauses load-bearing:
//   1. Fail closed. Absent key, wrong value, unavailable storage, a throwing
//      accessor (storage-locked webviews throw on a bare `localStorage` read)
//      all mean NOT CONSENTED. Only the exact literal below grants.
//   2. No I/O on the hot path. `usageConsentGranted()` is called once per
//      emitted event, so it reads a memory cache and never touches storage:
//      a synchronous localStorage read per event is a main-thread cost the
//      emitter must not pay. The cache is refreshed on write and on the
//      cross-tab `storage` event, which is every way the value can change.
//   3. Bounded listeners. The subscriber set is capped, and a listener that
//      throws can never break the caller that set consent.
//   4. Versioned key. The suffix below is bumped whenever the meaning of the
//      grant changes, which re-asks rather than silently reinterpreting an
//      old yes.

// Version suffix: bump to re-ask. `v1` = "usage-shaped product analytics".
export const USAGE_CONSENT_KEY = 'leaf.telemetry.usage_consent.v1'

// The ONLY value that means yes. A boolean-ish string ('1', 'true') would let
// an unrelated key collision or a half-written value read as consent.
const GRANTED = 'granted'

// A page cannot have thirty live Plan panels; a set that grows past this is a
// leak (a component subscribing without unsubscribing), not a use case.
const LISTENER_MAX = 32

function defaultStore() {
  // Reading the property itself can throw, so the guard wraps the READ.
  try {
    const store = globalThis?.localStorage
    return store && typeof store.getItem === 'function' ? store : null
  } catch { return null }
}

/** Pure read. Fails closed on every shape that is not the exact grant. */
export function readUsageConsentFrom(store) {
  try { return store?.getItem?.(USAGE_CONSENT_KEY) === GRANTED } catch { return false }
}

// Read ONCE at module evaluation; kept current by `setUsageConsent` and by the
// cross-tab listener below. See hardening clause 2.
let cached = readUsageConsentFrom(defaultStore())

const listeners = new Set()

function notify() {
  // A snapshot: a listener that unsubscribes (or subscribes) while we iterate
  // must not mutate the set we are walking.
  for (const fn of Array.from(listeners)) {
    try { fn(cached) } catch { /* a listener never breaks the setter */ }
  }
}

/** The hot-path read: no I/O, no allocation. */
export function usageConsentGranted() {
  return cached
}

/** Persist the viewer's choice and tell every subscriber. Returns the value
 * now in effect.
 *
 * Persistence is BEST EFFORT and the in-memory value is authoritative for this
 * tab: a viewer who just revoked must stop being measured immediately even
 * when the write throws (private mode, storage-locked webview), and a viewer
 * who just granted has consented for this session whether or not the yes
 * survives the reload. Both directions are honest; neither can collect
 * something the viewer refused. */
export function setUsageConsent(next, store = defaultStore()) {
  const value = next === true
  try {
    if (value) store?.setItem?.(USAGE_CONSENT_KEY, GRANTED)
    else store?.removeItem?.(USAGE_CONSENT_KEY)
  } catch { /* best effort; the in-memory value below still governs */ }
  if (cached !== value) {
    cached = value
    notify()
  }
  return cached
}

/** Re-read the store into the cache (cross-tab sync, and a test seam).
 * Returns the value now in effect. */
export function refreshUsageConsent(store = defaultStore()) {
  const value = readUsageConsentFrom(store)
  if (cached !== value) {
    cached = value
    notify()
  }
  return cached
}

/** Subscribe to changes. Returns an unsubscribe function — always call it;
 * the set is capped and a full set silently refuses new subscribers rather
 * than growing without bound. */
export function subscribeUsageConsent(listener) {
  if (typeof listener !== 'function') return () => {}
  if (listeners.size >= LISTENER_MAX && !listeners.has(listener)) return () => {}
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

// Revoking in one tab must stop collection in every tab of the same browser,
// which is the whole point of a per-browser grant. Installed once, guarded:
// a host with no addEventListener simply loses cross-tab sync, never boots.
try {
  if (typeof globalThis?.addEventListener === 'function') {
    globalThis.addEventListener('storage', (ev) => {
      try {
        if (ev && ev.key !== null && ev.key !== USAGE_CONSENT_KEY) return
        refreshUsageConsent()
      } catch { /* never break the page */ }
    })
  }
} catch { /* no-op */ }
