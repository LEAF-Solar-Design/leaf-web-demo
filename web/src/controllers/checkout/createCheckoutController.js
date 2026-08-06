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

export function deriveCheckout(
  checkout,
  holder,
  now = Date.now(),
  unknown = false,
  mock = false,
  hasCapability = false,
) {
  const lock = lockState({ mock, checkout, unknown, ownHolder: holder, nowMs: now })
  // A matching public holder label is not proof. After a reload or copied
  // sessionStorage, only the server-issued capability distinguishes the owner.
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
      publish({
        mock: !!next.mock,
        drawingId: next.drawingId || null,
        holder: next.holder || null,
        checkout: changed ? null : state.checkout,
        unknown: next.mock ? false : (changed ? true : state.unknown),
        readFailed: false,
        error: null,
      })
    },
    refresh,
    take: () => mutate('take'),
    takeDeferred: () => mutate('take', { installCapability: false }),
    release: () => mutate('release'),
    getCapability: () => capability,
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
