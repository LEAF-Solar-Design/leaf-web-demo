import { lockState } from '../../checkoutIdentity.js'

export function storeDrawingIdForSource(drawingSource) {
  return drawingSource === 'rooftop_demo' ? 'demo' : drawingSource
}

export function resolveCheckoutDrawingId({
  drawingState = null,
  requestedDrawingId = null,
} = {}) {
  return drawingState?.drawing_id || requestedDrawingId || null
}

/**
 * The checkout SCOPE for a surface, under the scope-reset contract
 * (docs/convergence/ACCEPTANCE.md, binding: "checkout state MUST reset on
 * tenant switch").
 *
 * A tenant switch is an auth-principal switch, and DrawingIdentityProvider
 * answers it by voiding every mode's drawing identity (`resetAll`), so
 * `identityDrawingId` goes null. The version chain does NOT reset with it:
 * `drawingState` still names the PREVIOUS tenant's drawing, so a scope that
 * read it alone would keep addressing that drawing — with the bearer
 * capability the previous principal was issued. That is exactly "a lock held
 * under tenant A gating and authorizing writes under tenant B".
 *
 * So a voided identity has NO checkout scope. Handing null to
 * `controller.setScope` is what drops the capability, clears the checkout
 * record and re-arms `unknown` — the ABANDON half of the answer.
 *
 * ABANDON, NOT RELEASE, deliberately. The capability was issued to the
 * previous principal and this client no longer holds that principal's token,
 * so a DELETE would either be refused (403) or be attributed to the NEW
 * principal. The server caps every lease at MAX_CHECKOUT_TTL_S and treats an
 * expired lock as free (see `isLegacyHolder` in checkoutIdentity.js), so the
 * abandoned lease drains on its own and any editor may Take it meanwhile.
 *
 * This GATES a scope, it never widens one: while the identity is present the
 * answer is `resolveCheckoutDrawingId`'s, unchanged, so each shell keeps the
 * exact drawing it addressed before.
 */
export function checkoutScopeDrawingId({
  identityDrawingId = null,
  drawingState = null,
  requestedDrawingId = null,
} = {}) {
  if (!identityDrawingId) return null
  return resolveCheckoutDrawingId({ drawingState, requestedDrawingId })
}

export function deriveCheckout(
  checkout,
  holder,
  now = Date.now(),
  unknown = false,
  mock = false,
  hasCapability = false,
) {
  const lock = lockState({ mock, checkout, unknown, ownHolder: holder, nowMs: now })
  // THE UNPROVEN-OWN-LOCK CORRECTION. `holder` is a PUBLIC label the manifest
  // hands to every reader, so a matching label is not proof of ownership: a
  // reload, a duplicated tab (browsers copy sessionStorage) or a redemption
  // this runtime lost all leave a client whose stored holder id equals the
  // lock's holder while it holds no capability at all. Only the server-issued
  // bearer capability distinguishes the owner.
  //
  // So a lock that LOOKS like ours but has no capability behind it is treated
  // as somebody else's: writes stay suppressed, no Release is offered for a
  // lease we cannot prove, and a Take is offered so the server can re-issue a
  // capability we can actually use. Both shells derive this here — the console
  // kept a hand-rolled twin of it until W2c and must never fork it again.
  const unprovenOwnLock = lock.heldByUs && !hasCapability ? checkout : null
  return {
    checkout,
    heldByUs: lock.heldByUs && hasCapability,
    lockedByOther: lock.otherHeld || unprovenOwnLock,
    staleByOther: lock.stale,
    legacyByOther: lock.legacy,
    canTake: lock.canTake || !!unprovenOwnLock,
    unknown: lock.unknown,
    writeLocked: lock.writeLocked || !!unprovenOwnLock,
  }
}

export function createCheckoutController({ mock = false, drawingId = null, holder = null, services, now = Date.now } = {}) {
  if (!services) throw new TypeError('createCheckoutController requires services')
  let state = {
    mock: !!mock,
    drawingId,
    holder,
    checkout: null,
    busy: false,
    error: null,
    unknown: !mock,
    readFailed: false,
  }
  // Issued once by a successful take. It is a bearer credential, so keep it
  // outside snapshots and React state, and never persist it.
  let capability = null
  let disposed = false
  let refreshSeq = 0
  // Monotonic per setScope CHANGE: async work fences itself on this so a
  // result that raced a tenant/drawing switch can prove its scope is stale.
  let scopeGeneration = 0
  let mutationBusy = false
  const listeners = new Set()
  let snapshotState = {
    ...state,
    ...deriveCheckout(
      state.checkout,
      state.holder,
      now(),
      state.unknown,
      state.mock,
      !!capability,
    ),
  }
  const publish = (patch) => {
    if (disposed) return
    state = { ...state, ...patch }
    snapshotState = {
      ...state,
      ...deriveCheckout(
        state.checkout,
        state.holder,
        now(),
        state.unknown,
        state.mock,
        !!capability,
      ),
    }
    listeners.forEach((listener) => listener())
  }

  const refresh = async () => {
    const seq = ++refreshSeq
    const drawing = state.drawingId
    if (state.mock || !drawing) {
      publish({ checkout: null, error: null, unknown: false, readFailed: false })
      return null
    }
    publish({ unknown: true, readFailed: false, error: null })
    try {
      const manifest = await services.loadVersions(drawing)
      if (disposed || seq !== refreshSeq || drawing !== state.drawingId) return null
      publish({
        checkout: manifest?.checkout || null,
        error: null,
        unknown: false,
        readFailed: false,
      })
      return manifest?.checkout || null
    } catch (error) {
      if (disposed || seq !== refreshSeq || drawing !== state.drawingId) return null
      publish({
        checkout: null,
        error: String(error?.message || error),
        unknown: true,
        readFailed: true,
      })
      return null
    }
  }

  const mutate = async (operation, { installCapability = true } = {}) => {
    if (state.mock || !state.drawingId || mutationBusy) return null
    mutationBusy = true
    const drawing = state.drawingId
    const holderAtStart = state.holder
    publish({ busy: true, error: null })
    try {
      const result = operation === 'take'
        ? await services.take(drawing, holderAtStart, capability)
        : await services.release(drawing, capability)
      if (operation === 'take') {
        if (result?.acquired && result.checkout_capability) {
          capability = installCapability ? result.checkout_capability : null
        }
        else if (!result?.acquired) capability = null
      } else {
        capability = null
      }
      return result
    } catch (error) {
      if (error?.status === 403 || error?.status === 409) capability = null
      if (!disposed && drawing === state.drawingId) publish({ error: String(error?.message || error) })
      return null
    } finally {
      if (!disposed && drawing === state.drawingId) await refresh()
      mutationBusy = false
      if (!disposed && drawing === state.drawingId) publish({ busy: false })
    }
  }

  return {
    getSnapshot: () => snapshotState,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    start() { disposed = false },
    dispose() { disposed = true; refreshSeq += 1; listeners.clear() },
    setScope(next = {}) {
      const changed =
        next.drawingId !== state.drawingId ||
        next.holder !== state.holder ||
        !!next.mock !== state.mock
      if (changed) refreshSeq += 1
      if (changed) capability = null
      if (changed) scopeGeneration += 1
      publish({
        mock: !!next.mock,
        drawingId: next.drawingId || null,
        holder: next.holder || null,
        checkout: changed ? null : state.checkout,
        unknown: next.mock ? false : (changed ? true : state.unknown),
        readFailed: false,
        error: null,
        // mutate()'s finally guards its busy:false on the OLD drawingId, so a
        // scope change mid-mutation would strand busy forever (panel W2c,
        // lock-safety WARN): the new scope starts un-busy by definition.
        busy: changed ? false : state.busy,
      })
    },
    refresh,
    take: () => mutate('take'),
    takeDeferred: () => mutate('take', { installCapability: false }),
    release: () => mutate('release'),
    getCapability: () => capability,
    getScopeGeneration: () => scopeGeneration,
    restoreCapability(nextCapability) {
      if (state.mock || !state.drawingId || !state.holder ||
          typeof nextCapability !== 'string' || !nextCapability) return false
      capability = nextCapability
      publish({})
      return true
    },
    clearCapability() {
      capability = null
      publish({})
    },
  }
}

export default createCheckoutController
