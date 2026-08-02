// Who "we" are for the single-writer checkout lock.
//
// The lock exists to stop two editors writing the same drawing at once, so the
// holder id has to name a SESSION, not an organization. Deriving it from the
// tenant made every teammate in an org look like the same holder: when A took
// the lock, B's client saw `checkout.holder === ownHolder` too, so B was told
// "You hold the edit lock" and was handed a Release button for a lock B never
// took. The single-writer guarantee was void for exactly the multi-user case it
// exists to protect.
//
// The id is persisted in sessionStorage so it survives F5 (one editor reloading
// keeps their lock).
//
// WHY sessionStorage ALONE IS NOT ENOUGH. The first version of this file claimed
// "sessionStorage is scoped to one tab (two tabs = two editors = two ids)". That
// is true for a tab the user OPENS, and false for a tab the user DUPLICATES:
// Chrome, Firefox and Safari all copy the sessionStorage contents into the new
// tab. So the duplicate inherited the stored id and both tabs believed they held
// the lock, which is the same defect as the tenant version with a new trigger.
//
// Storage alone CANNOT fix this, and that is the whole design constraint here: a
// reload and a duplication present identically. Both start a fresh runtime that
// finds a stored id it did not mint this run. The only thing that distinguishes
// them is whether the runtime that stored the id is STILL ALIVE, which no
// synchronous storage read can answer. So we ask, over a BroadcastChannel: the
// incumbent answers, and the newcomer remints. See `claimHolderId`.
//
// PURE by contract: no bundler-only globals, no React, no JSON imports, so `node`
// can import it headless (web/scripts/check_checkout_identity.mjs does exactly
// that) by passing a storage stub and a channel stub.

export const HOLDER_STORAGE_KEY = 'leaf.checkout_holder'
export const HOLDER_CHANNEL_NAME = 'leaf.checkout_holder_claim'
export const CHECKOUT_RELOAD_HANDOFF_KEY = 'leaf.checkout_reload_handoff'
export const CHECKOUT_RELOAD_HANDOFF_MAX_AGE_MS = 30_000

// Last-resort id for when there is no usable storage at all (SSR, or a browser
// with storage disabled). Module-level so repeated calls in one runtime agree
// rather than minting a new holder on every render.
let memoryHolderId = null
let runtimeReloadHandoff = undefined
let runtimeReloadHandoffScope = null
const runtimeAuthorityLocks = new Map()

function defaultStorage() {
  return typeof sessionStorage !== 'undefined' ? sessionStorage : null
}

function defaultChannel() {
  if (typeof BroadcastChannel === 'undefined') return null
  try {
    return new BroadcastChannel(HOLDER_CHANNEL_NAME)
  } catch {
    return null
  }
}

// A checkout capability is deliberately not kept in normal browser storage.
// A reload is the one lifecycle edge where memory-only authority would strand
// its still-live one-hour lease. The outgoing page therefore writes a short,
// one-use handoff during beforeunload. The receiving runtime must NOT use this
// capability directly. App.jsx first acquires an origin-wide exclusive Web Lock
// for this holder and drawing. If sessionStorage cloning gives two runtimes the
// handoff, the browser serializes them and only the lock owner may install it.
export function stageCheckoutReloadHandoff({
  capability,
  holder,
  drawingId,
  storage = defaultStorage(),
  now = () => Date.now(),
} = {}) {
  if (!storage || typeof capability !== 'string' || !capability ||
      typeof holder !== 'string' || !holder ||
      typeof drawingId !== 'string' || !drawingId) return false
  try {
    storage.setItem(CHECKOUT_RELOAD_HANDOFF_KEY, JSON.stringify({
      v: 1,
      capability,
      holder,
      drawing_id: drawingId,
      created_at_ms: now(),
    }))
    return true
  } catch {
    return false
  }
}

export function consumeCheckoutReloadHandoff({
  holder,
  drawingId,
  storage = defaultStorage(),
  now = () => Date.now(),
  maxAgeMs = CHECKOUT_RELOAD_HANDOFF_MAX_AGE_MS,
} = {}) {
  if (!storage) return null
  let raw = null
  try {
    raw = storage.getItem(CHECKOUT_RELOAD_HANDOFF_KEY)
    // Consume before parsing or validating. A corrupt, mismatched, or replayed
    // handoff must never remain available to a later runtime.
    storage.removeItem(CHECKOUT_RELOAD_HANDOFF_KEY)
  } catch {
    return null
  }
  if (!raw) return null
  try {
    const handoff = JSON.parse(raw)
    const age = now() - Number(handoff?.created_at_ms)
    if (handoff?.v !== 1 || handoff.holder !== holder ||
        handoff.drawing_id !== drawingId ||
        typeof handoff.capability !== 'string' || !handoff.capability ||
        !Number.isFinite(age) || age < 0 || age > maxAgeMs) return null
    return {
      capability: handoff.capability,
      holder,
      drawingId,
      createdAtMs: Number(handoff.created_at_ms),
    }
  } catch {
    return null
  }
}

// React may render a component speculatively and discard that render. Keep the
// destructive, one-use storage consume behind a module-runtime cache so every
// render for the same scope receives the same provisional handoff. The cache
// dies with the JavaScript runtime and never becomes durable authority.
export function bootstrapCheckoutReloadHandoff({
  holder,
  drawingId,
  storage = defaultStorage(),
  now = () => Date.now(),
  navigationType = null,
} = {}) {
  const navType = navigationType || (() => {
    try { return performance.getEntriesByType('navigation')?.[0]?.type || 'navigate' } catch { return 'navigate' }
  })()
  const scope = `${String(holder || '')}\u0000${String(drawingId || '')}\u0000${navType}`
  if (runtimeReloadHandoff !== undefined && runtimeReloadHandoffScope === scope) {
    return runtimeReloadHandoff
  }
  runtimeReloadHandoffScope = scope
  const consumed = consumeCheckoutReloadHandoff({ holder, drawingId, storage, now })
  // A duplicated/new tab reports "navigate", not "reload". Its cloned storage
  // must never transfer checkout authority, even if it copied a valid handoff.
  runtimeReloadHandoff = navType === 'reload' ? consumed : null
  return runtimeReloadHandoff
}

function defaultLockManager() {
  return typeof navigator !== 'undefined' ? navigator.locks : null
}

export function checkoutAuthorityLockName({ holder, drawingId } = {}) {
  return `leaf.checkout.authority:${String(holder || '')}:${String(drawingId || '')}`
}

// Start an exclusive, runtime-lifetime lock without awaiting its release. The
// returned controller is active only when Web Locks exist; callers fail closed
// otherwise. A queued duplicate receives authority only after the prior runtime
// releases or unloads, so two tabs never install the same capability together.
export function holdCheckoutReloadAuthority({
  handoff,
  locks = defaultLockManager(),
  onAcquired = null,
  onError = null,
  now = () => Date.now(),
  maxAgeMs = CHECKOUT_RELOAD_HANDOFF_MAX_AGE_MS,
} = {}) {
  if (!handoff?.capability || !handoff?.holder || !handoff?.drawingId ||
      !locks || typeof locks.request !== 'function') {
    return { active: false, stop() {}, acquired: Promise.resolve(false), done: Promise.resolve() }
  }
  const lockName = checkoutAuthorityLockName(handoff)
  const existing = runtimeAuthorityLocks.get(lockName)
  if (existing) return existing
  let release = null
  let stopped = false
  let settleAcquired = null
  const acquired = new Promise((resolve) => { settleAcquired = resolve })
  let requestResult = null
  try {
    requestResult = locks.request(
    lockName,
    { mode: 'exclusive' },
    async () => {
      if (stopped) { settleAcquired(false); return }
      if (Number.isFinite(handoff.createdAtMs)) {
        const age = now() - handoff.createdAtMs
        if (!Number.isFinite(age) || age < 0 || age > maxAgeMs) {
          settleAcquired(false)
          if (typeof onError === 'function') onError(new Error('checkout reload handoff expired while waiting'))
          return
        }
      }
      if (typeof onAcquired === 'function') onAcquired(handoff)
      settleAcquired(true)
      await new Promise((resolve) => { release = resolve })
    })
  } catch (error) {
    settleAcquired(false)
    if (typeof onError === 'function') onError(error)
    return { active: false, stop() {}, acquired, done: Promise.resolve() }
  }
  const done = Promise.resolve(requestResult).catch((error) => {
    settleAcquired(false)
    if (!stopped && typeof onError === 'function') onError(error)
  })
  const controller = {
    active: true,
    acquired,
    done,
    stop() {
      stopped = true
      settleAcquired(false)
      release?.()
      if (runtimeAuthorityLocks.get(lockName) === controller) runtimeAuthorityLocks.delete(lockName)
    },
  }
  runtimeAuthorityLocks.set(lockName, controller)
  return controller
}

// A holder id that cannot collide with a tenant/org string by construction:
// prefixed, and random per session. Mirrors the `globalThis.crypto?.randomUUID`
// idiom already used for run-intent session ids in App.jsx.
export function mintHolderId() {
  const uuid = globalThis.crypto?.randomUUID?.()
  if (uuid) return `sess-${uuid}`
  // No crypto (old browser / odd runtime): still unique enough for a per-tab id.
  const rand = () => Math.random().toString(36).slice(2, 10)
  return `sess-${rand()}${rand()}-${Date.now().toString(36)}`
}

export function remintSessionHolderId(storage = defaultStorage()) {
  const next = mintHolderId()
  memoryHolderId = next
  if (storage) {
    try { storage.setItem(HOLDER_STORAGE_KEY, next) } catch { /* keep in memory */ }
  }
  return next
}

function memoryHolder() {
  if (!memoryHolderId) memoryHolderId = mintHolderId()
  return memoryHolderId
}

/**
 * getSessionHolderId(storage?) -> a stable per-session holder id.
 *
 * Reads the id from `storage`, minting and persisting one on first call. The
 * storage is injectable so this is testable outside a browser. A missing or
 * unusable storage (private mode can throw on access) falls back to a
 * module-level in-memory id, so this function never throws.
 *
 * This is deliberately SYNCHRONOUS so the first render has an id. It cannot
 * detect a duplicated tab on its own; pair it with `claimHolderId`.
 */
export function getSessionHolderId(storage = defaultStorage()) {
  if (!storage) return memoryHolder()

  try {
    const existing = storage.getItem(HOLDER_STORAGE_KEY)
    if (typeof existing === 'string' && existing.trim()) return existing

    const minted = mintHolderId()
    storage.setItem(HOLDER_STORAGE_KEY, minted)
    return minted
  } catch {
    // Storage present but refusing reads/writes: degrade, don't crash.
    return memoryHolder()
  }
}

/**
 * claimHolderId({id, storage?, channel?, onRemint?, now?}) -> stop()
 *
 * Announces `id` on the claim channel. If another LIVE runtime already holds it
 * (the duplicated-tab case), that runtime answers and we remint, persist the new
 * id, and call `onRemint(newId)` so the UI stops impersonating the incumbent.
 *
 * Tie-break is by claim age: the runtime that claimed EARLIER keeps the id, so
 * exactly one side remints. Equal timestamps fall back to comparing a random
 * per-claim nonce, so two runtimes that start in the same millisecond still
 * resolve to one winner rather than both reminting or both keeping.
 *
 * With no BroadcastChannel available the claim is a no-op and duplicate-tab
 * detection is simply absent (documented degradation, not silent failure: the
 * returned object reports `active: false`).
 */
export function claimHolderId({
  id,
  storage = defaultStorage(),
  channel = defaultChannel(),
  onRemint = null,
  now = () => Date.now(),
  // The first announce is deferred by a task, not sent inline. Observed in
  // Chrome: a BroadcastChannel posted to in the SAME tick it was constructed can
  // drop the message, so an inline claim is silently lost and the duplicate tab
  // keeps the incumbent's id. A stub channel cannot reproduce this, which is why
  // it survived the headless check; the injectable scheduler lets that check stay
  // synchronous while production yields first.
  schedule = (fn) => setTimeout(fn, 0),
} = {}) {
  if (!id || !channel) return { active: false, stop() {} }

  let currentId = id
  const claimedAt = now()
  const nonce = mintHolderId()

  const post = (msg) => {
    try { channel.postMessage(msg) } catch { /* channel closed; nothing to do */ }
  }

  const remint = () => {
    const next = mintHolderId()
    currentId = next
    if (storage) {
      try { storage.setItem(HOLDER_STORAGE_KEY, next) } catch { /* keep in memory */ }
    }
    memoryHolderId = next
    if (typeof onRemint === 'function') onRemint(next)
    // Announce the new id too, in case a third runtime shares it.
    post({ type: 'claim', id: next, claimedAt, nonce })
  }

  // Total order over runtimes: earlier claim wins, and an exact timestamp tie
  // falls back to the random per-claim nonce. Total matters more than fair here.
  // Any rule that can return "cannot decide" leaves two live runtimes sharing an
  // id, which is the whole defect being fixed.
  const isOlderThanUs = (msg) =>
    msg.claimedAt < claimedAt ||
    (msg.claimedAt === claimedAt && String(msg.nonce) < nonce)

  const onMessage = (ev) => {
    const msg = ev?.data
    if (!msg || msg.id !== currentId) return

    if (msg.type === 'claim') {
      // Someone else claims an id we are holding, so we say we hold it, and we
      // include OUR age so the claimant can tell whether we outrank it.
      post({ type: 'held', id: currentId, claimedAt, nonce })
      return
    }

    if (msg.type === 'held') {
      // Step aside only for a runtime that was here BEFORE us.
      //
      // An earlier version reminted on any `held`, which loses the incumbent.
      // With three tabs sharing an id, B and C claim at once; A answers both, but
      // B answers C's claim too (B still holds the id at that instant), and A
      // then obeys B's answer and remints. All three end up on fresh ids and the
      // server-side lock A actually took is orphaned until it expires, with no
      // client able to release it. Comparing age makes the outcome total and
      // deterministic: the oldest runtime keeps the id and every newer one moves.
      if (isOlderThanUs(msg)) remint()
    }
  }

  try {
    channel.addEventListener?.('message', onMessage)
    if (!channel.addEventListener) channel.onmessage = onMessage
  } catch {
    return { active: false, stop() {} }
  }

  schedule(() => post({ type: 'claim', id: currentId, claimedAt, nonce }))

  return {
    active: true,
    get id() { return currentId },
    stop() {
      try {
        channel.removeEventListener?.('message', onMessage)
        channel.close?.()
      } catch { /* already gone */ }
    },
  }
}

/**
 * isLegacyHolder(holder) -> true when `holder` was minted before this module
 * existed. The server defaults `holder` to the tenant id when a client sends
 * none (routers/drawings.py: `holder = req.holder or str(tenant_id)`), so live
 * locks in the wild carry tenant-shaped holders. Those are NOT ours, and the
 * server caps every lease at MAX_CHECKOUT_TTL_S (24h) and treats an expired
 * lock as free, so honouring `expires` is what lets them drain. This helper
 * exists so the UI can say "held by an older client" rather than nothing.
 */
export function isLegacyHolder(holder) {
  return typeof holder === 'string' && holder.trim() !== '' && !holder.startsWith('sess-')
}

/**
 * hasHolder(checkout) -> is a holder recorded at all?
 *
 * This, and NOT expiry, is what suppresses writes. See `looksStale` for why the
 * browser clock is deliberately kept out of that decision.
 */
export function hasHolder(checkout) {
  return !!(checkout && typeof checkout.holder === 'string' && checkout.holder.trim())
}

/**
 * looksStale(checkout, nowMs?) -> should we OFFER to take this lock?
 *
 * The browser clock is not an authority on whether a lease has elapsed, and an
 * earlier version of this file made it one: it returned "free" whenever
 * `expires <= Date.now()`, so an editor whose clock ran two hours fast decided a
 * live lock was free and re-enabled its own writes. Skew is not hypothetical on
 * shared machines, and no margin fixes a two-hour error.
 *
 * So expiry no longer decides anything. It only decides what we OFFER: when a
 * lease looks elapsed, or carries an `expires` we cannot read, we show a Take
 * action and let the SERVER adjudicate. The server already owns this question
 * ("an EXPIRED lock is free, re-acquirable by anyone", and its fenced write
 * requires `checkout_expires_at > clock_timestamp()`), so a Take either succeeds
 * because the server agrees, or returns 409 and the refetch shows the truth.
 *
 * Missing or unparseable `expires` counts as stale for this purpose. Treating it
 * as indefinitely held is what wedged the UI: a `{holder:"legacy", expires:"bad"}`
 * record left a permanent client-side lock with no Take and no Release. Offering
 * a Take cannot lose data, because the server rejects a take it disagrees with.
 */
export function looksStale(checkout, nowMs = Date.now()) {
  if (!hasHolder(checkout)) return false
  const raw = checkout.expires
  if (raw === undefined || raw === null || raw === '') return true
  const t = Date.parse(raw)
  if (Number.isNaN(t)) return true
  return t <= nowMs
}

/**
 * lockState({mock, checkout, unknown, ownHolder, nowMs?}) -> the whole write-lock
 * decision, as data.
 *
 * This lives here, not inline in the component, so the oracle can EXERCISE it
 * instead of grepping App.jsx for the presence of a token. The previous check
 * asserted things like /setCheckoutUnknown\(true\)/ against the source, which an
 * unreachable call satisfies just as well as a correct one, so the oracle was not
 * load-bearing. Every rule below is now a behaviour a test can pin:
 *
 *   - `unknown` (no answer yet, or the read failed) suppresses writes. Fail closed.
 *   - a recorded holder that is not us suppresses writes, regardless of expiry.
 *   - expiry never enables a write; it only sets `stale`, which offers a Take that
 *     the server adjudicates.
 *   - mock mode holds no locks at all.
 */
export function lockState({ mock, checkout, unknown, ownHolder, nowMs = Date.now() }) {
  if (mock) {
    return { writeLocked: false, heldByUs: false, otherHeld: null, stale: false, legacy: false, unknown: false }
  }
  const held = hasHolder(checkout) ? checkout : null
  const heldByUs = !!(held && held.holder === ownHolder)
  const otherHeld = held && !heldByUs ? held : null
  return {
    writeLocked: !!otherHeld || !!unknown,
    heldByUs,
    otherHeld,
    // A Take is offered whenever SOMEONE ELSE holds it, with no reference to the
    // clock. Gating the offer on `stale` still let the browser clock decide, just
    // in the other direction: a clock running two hours SLOW judged an elapsed
    // lease live, hid the Take, and left the user unable to write or take for two
    // hours. The server is the only authority, and it already rejects a take it
    // disagrees with (409), after which the refetch shows the truth. So the worst
    // case for offering a Take too eagerly is a harmless rejected request, while
    // the worst case for hiding it is a wedge no user action can clear.
    canTake: !!otherHeld,
    // Display only, and clock-derived, so it must never gate an action.
    stale: !!otherHeld && looksStale(otherHeld, nowMs),
    legacy: !!otherHeld && isLegacyHolder(otherHeld.holder),
    unknown: !!unknown,
  }
}

export default getSessionHolderId
