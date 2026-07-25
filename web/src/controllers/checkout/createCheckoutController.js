function activeCheckout(checkout, now = Date.now()) {
  if (!checkout?.holder) return null
  const expiry = checkout.expires ? new Date(checkout.expires).getTime() : null
  if (Number.isFinite(expiry) && expiry <= now) return null
  return checkout
}

export function deriveCheckout(checkout, holder, now = Date.now()) {
  const active = activeCheckout(checkout, now)
  const heldByUs = !!active && active.holder === holder
  const lockedByOther = active && !heldByUs ? active : null
  return { checkout: active, heldByUs, lockedByOther, writeLocked: !!lockedByOther }
}

export function createCheckoutController({ mock = false, drawingId = null, holder = null, services, now = Date.now } = {}) {
  if (!services) throw new TypeError('createCheckoutController requires services')
  let state = { mock: !!mock, drawingId, holder, checkout: null, busy: false, error: null }
  let disposed = false
  let refreshSeq = 0
  let mutationBusy = false
  const listeners = new Set()
  let snapshotState = { ...state, ...deriveCheckout(state.checkout, state.holder, now()) }
  const publish = (patch) => {
    if (disposed) return
    state = { ...state, ...patch }
    snapshotState = { ...state, ...deriveCheckout(state.checkout, state.holder, now()) }
    listeners.forEach((listener) => listener())
  }

  const refresh = async () => {
    const seq = ++refreshSeq
    const drawing = state.drawingId
    if (state.mock || !drawing) {
      publish({ checkout: null, error: null })
      return null
    }
    try {
      const manifest = await services.loadVersions(drawing)
      if (disposed || seq !== refreshSeq || drawing !== state.drawingId) return null
      publish({ checkout: manifest?.checkout || null, error: null })
      return manifest?.checkout || null
    } catch (error) {
      if (disposed || seq !== refreshSeq || drawing !== state.drawingId) return null
      publish({ checkout: null, error: String(error?.message || error) })
      return null
    }
  }

  const mutate = async (operation) => {
    if (state.mock || !state.drawingId || mutationBusy) return null
    mutationBusy = true
    const drawing = state.drawingId
    const holderAtStart = state.holder
    publish({ busy: true, error: null })
    try {
      const result = operation === 'take'
        ? await services.take(drawing, holderAtStart)
        : await services.release(drawing, holderAtStart)
      return result
    } catch (error) {
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
      const changed = next.drawingId !== state.drawingId || !!next.mock !== state.mock
      if (changed) refreshSeq += 1
      publish({
        mock: !!next.mock,
        drawingId: next.drawingId || null,
        holder: next.holder || null,
        checkout: changed ? null : state.checkout,
        error: null,
      })
    },
    refresh,
    take: () => mutate('take'),
    release: () => mutate('release'),
  }
}

export default createCheckoutController
