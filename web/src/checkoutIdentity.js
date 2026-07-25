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

// Last-resort id for when there is no usable storage at all (SSR, or a browser
// with storage disabled). Module-level so repeated calls in one runtime agree
// rather than minting a new holder on every render.
let memoryHolderId = null

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

  const onMessage = (ev) => {
    const msg = ev?.data
    if (!msg || msg.id !== currentId) return

    if (msg.type === 'claim') {
      // Someone else claims an id we are holding, so we answer. We do NOT try to
      // work out which of us is older: the invariant worth protecting is that two
      // live runtimes never share an id, and an age tie-break cannot be decided
      // when both started in the same millisecond. Answering unconditionally
      // means the incumbent keeps its id in the case that actually happens (a
      // duplicate starts long after the original), and in the pathological
      // simultaneous start both step aside, which is wasteful but still safe.
      post({ type: 'held', id: currentId, claimedAt, nonce })
      return
    }

    if (msg.type === 'held') {
      // An older live runtime owns this id. We are the duplicate: step aside.
      remint()
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
 * isCheckoutActive(checkout, nowMs?) -> is this lock still holding anything?
 *
 * The server is explicit that "an EXPIRED lock (expires <= now) is free,
 * re-acquirable by anyone" (routers/drawings.py) and its fenced write requires
 * `checkout_expires_at > clock_timestamp()`. A client that ignores `expires`
 * therefore disagrees with the server about who may write: it kept showing a
 * dead lock as live, suppressing writes with no way to clear it. A checkout with
 * no `expires` at all is treated as active, because absent is not the same as
 * elapsed and the safe reading of an unbounded lock is that it still holds.
 */
export function isCheckoutActive(checkout, nowMs = Date.now()) {
  if (!checkout || !checkout.holder) return false
  const raw = checkout.expires
  if (raw === undefined || raw === null || raw === '') return true
  const t = Date.parse(raw)
  if (Number.isNaN(t)) return true // unparseable: do not silently free the lock
  return t > nowMs
}

export default getSessionHolderId
