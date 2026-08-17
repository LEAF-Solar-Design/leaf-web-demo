const initialState = Object.freeze({
  status: 'checking',
  reason: null,
  sources: [],
  generation: 0,
  session: null,
  recoveries: 0,
})

// A 401 latches `required` and nothing re-runs getSession, which is correct for
// a real expiry and WRONG for the post-callback window: the SPA's mount burst
// can beat the token write, so eleven 401s strand a page that holds a valid
// token behind a signed-out-looking surface until a manual reload (the
// 2026-08-17 D1a defect #6 ALB timeline: burst 18:49:56.29Z, token 700ms later,
// /api/session 200 in 0.4s, tabs disabled for the full 120s).
//
// Recovery is bounded and edge-triggered, never a poll: only a token that is
// PRESENT and DIFFERENT from the one the refusal latched on re-opens `checking`,
// at most MAX_TOKEN_RECOVERIES times for the life of the controller. A wrong
// token that keeps 401ing therefore costs a fixed, small number of round trips
// and then stays latched, and an explicit sign-out is never re-opened at all.
export const MAX_TOKEN_RECOVERIES = 3

export function createSessionController({
  storage,
  subscribeUnauthorized,
  subscribeTokenStored,
  endSession,
} = {}) {
  let state = initialState
  let unsubscribeUnauthorized = null
  let unsubscribeTokenStored = null
  // The token in play when the latest refusal landed. Null when the refusal
  // arrived with no token stored at all, which is exactly the race window.
  let latchedToken = null
  const listeners = new Set()

  const publish = (next) => {
    if (next === state) return
    state = next
    for (const listener of listeners) listener()
  }

  const readToken = () => {
    try { return storage?.getItem('leaf.jwt') || null } catch { return null }
  }

  const clearToken = () => {
    try { storage?.removeItem('leaf.jwt') } catch { /* storage unavailable */ }
  }

  const requireAuth = (source = 'unknown') => {
    latchedToken = readToken()
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

  // The ONE deliberate exception to `checking()`'s refusal to leave `required`.
  // Returns true when it re-opened the gate, so the caller re-runs getSession.
  const recoverWithStoredToken = () => {
    if (state.status !== 'required') return false
    if (state.reason === 'signed_out') return false
    if (state.recoveries >= MAX_TOKEN_RECOVERIES) return false
    const token = readToken()
    if (!token || token === latchedToken) return false
    latchedToken = token
    publish({
      ...state,
      status: 'checking',
      reason: null,
      sources: [],
      session: null,
      recoveries: state.recoveries + 1,
    })
    return true
  }

  const activate = (session) => {
    publish({
      status: 'active',
      reason: null,
      sources: [],
      generation: state.generation + 1,
      session: session || null,
      recoveries: state.recoveries,
    })
    return state
  }

  const signOut = async () => {
    try { storage?.removeItem('leaf.inflightAuthor.v1') } catch { /* storage unavailable */ }
    requireAuth('signout')
    await endSession?.()
  }

  const start = () => {
    if (!unsubscribeUnauthorized && subscribeUnauthorized) {
      unsubscribeUnauthorized = subscribeUnauthorized((source) => requireAuth(source || 'api:401'))
    }
    if (!unsubscribeTokenStored && subscribeTokenStored) {
      unsubscribeTokenStored = subscribeTokenStored(() => { recoverWithStoredToken() })
    }
  }

  const destroy = () => {
    unsubscribeUnauthorized?.()
    unsubscribeUnauthorized = null
    unsubscribeTokenStored?.()
    unsubscribeTokenStored = null
  }

  return {
    getSnapshot: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    start,
    destroy,
    actions: { checking, activate, requireAuth, signOut, recoverWithStoredToken },
  }
}

export default createSessionController
