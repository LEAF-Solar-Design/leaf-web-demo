const initialState = Object.freeze({
  status: 'checking',
  reason: null,
  sources: [],
  generation: 0,
  session: null,
})

export function createSessionController({ storage, subscribeUnauthorized, endSession } = {}) {
  let state = initialState
  let unsubscribeUnauthorized = null
  const listeners = new Set()

  const publish = (next) => {
    if (next === state) return
    state = next
    for (const listener of listeners) listener()
  }

  const clearToken = () => {
    try { storage?.removeItem('leaf.jwt') } catch { /* storage unavailable */ }
  }

  const requireAuth = (source = 'unknown') => {
    clearToken()
    const sources = state.sources.includes(source) ? state.sources : [...state.sources, source]
    if (state.status === 'required' && sources === state.sources) return state
    publish({
      ...state,
      status: 'required',
      reason: source === 'signout' ? 'signed_out' : 'expired',
      sources,
      session: null,
    })
    return state
  }

  const checking = () => {
    if (state.status === 'required') return state
    publish({ ...state, status: 'checking', reason: null, session: null })
    return state
  }

  const activate = (session) => {
    publish({
      status: 'active',
      reason: null,
      sources: [],
      generation: state.generation + 1,
      session: session || null,
    })
    return state
  }

  const signOut = async () => {
    requireAuth('signout')
    await endSession?.()
  }

  const start = () => {
    if (unsubscribeUnauthorized || !subscribeUnauthorized) return
    unsubscribeUnauthorized = subscribeUnauthorized((source) => requireAuth(source || 'api:401'))
  }

  const destroy = () => {
    unsubscribeUnauthorized?.()
    unsubscribeUnauthorized = null
  }

  return {
    getSnapshot: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    start,
    destroy,
    actions: { checking, activate, requireAuth, signOut },
  }
}

export default createSessionController
