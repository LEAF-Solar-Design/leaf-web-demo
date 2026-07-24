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
// The id is per-tab and stable across reloads: sessionStorage is scoped to one
// tab (two tabs = two editors = two ids) and survives F5 (one editor reloading
// keeps their lock).
//
// PURE by contract: no bundler-only globals, no React, no JSON imports, so `node`
// can import it headless (web/scripts/check_checkout_identity.mjs does exactly
// that) by passing a storage stub.

export const HOLDER_STORAGE_KEY = 'leaf.checkout_holder'

// Last-resort id for when there is no usable storage at all (SSR, or a browser
// with storage disabled). Module-level so repeated calls in one runtime agree
// rather than minting a new holder on every render.
let memoryHolderId = null

function defaultStorage() {
  return typeof sessionStorage !== 'undefined' ? sessionStorage : null
}

// A holder id that cannot collide with a tenant/org string by construction:
// prefixed, and random per session. Mirrors the `globalThis.crypto?.randomUUID`
// idiom already used for run-intent session ids in App.jsx.
function mintHolderId() {
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

export default getSessionHolderId
