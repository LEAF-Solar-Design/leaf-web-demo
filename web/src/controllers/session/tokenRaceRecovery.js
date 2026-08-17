// Bounded recovery from the post-callback token race (2026-08-17 UPDATE 9).
//
// On a fresh login the first authed mount can fire /api/session BEFORE
// handleRedirectCallback persists leaf.jwt: the pre-token 401 latched the
// session controller into 'required', nothing retried, and the user saw a
// signed-out-looking /try (dead Trust/Jobs tabs) while actually signed in —
// until a manual reload. ALB proof: burst 18:49:56.29Z, token present 700ms
// later, /api/session 200 in 0.4s.
//
// This module owns the 401 decision for a session probe:
//   - request carried a bearer            -> latch (a real rejection; api.js
//     noteUnauthorized already wiped a genuinely rejected stored token)
//   - token appeared while in flight      -> retry now (bounded)
//   - no token but a code exchange is in
//     flight in this document             -> recheck on a short fixed
//     schedule, retry at first sighting, latch when it never lands
//   - anything else                       -> latch (fail closed)
//
// Hard bounds, no busy loop: at most one pending timer, at most
// RECHECK_DELAYS_MS.length rechecks per 401, at most maxRecoveries
// retries per instance lifetime. Latching (the calm signed-out gate) is
// always the terminal state when recovery is not provably safe.

// Successive setTimeout delays; cumulative 250ms / 1s / 3.25s — covers the
// observed 700ms token gap with margin while keeping the give-up bounded.
export const RECHECK_DELAYS_MS = Object.freeze([250, 750, 2250])

export function createTokenRaceRecovery({
  isSignedIn,
  isCallbackInFlight,
  maxRecoveries = 2,
  setTimeoutFn = (fn, ms) => setTimeout(fn, ms),
  clearTimeoutFn = (id) => clearTimeout(id),
} = {}) {
  if (typeof isSignedIn !== 'function' || typeof isCallbackInFlight !== 'function') {
    throw new TypeError('createTokenRaceRecovery requires isSignedIn and isCallbackInFlight')
  }
  let recoveries = 0
  let timer = null
  let disposed = false

  const cancel = () => {
    if (timer !== null) { clearTimeoutFn(timer); timer = null }
  }

  // Decide synchronously on a 401 from a session probe. onRetry/onLatch are
  // per-call so the caller can bind its own liveness guards; exactly one of
  // them fires per handle401 (onRetry/onLatch asynchronously for 'recheck').
  const handle401 = ({ hadTokenAtSend, onRetry, onLatch }) => {
    if (disposed) return 'disposed'
    cancel() // a new 401 decision supersedes any pending recheck chain
    if (hadTokenAtSend) { onLatch(); return 'latch' }
    if (isSignedIn()) {
      if (recoveries >= maxRecoveries) { onLatch(); return 'latch' }
      recoveries += 1
      onRetry()
      return 'retry'
    }
    if (!isCallbackInFlight() || recoveries >= maxRecoveries) { onLatch(); return 'latch' }
    recoveries += 1
    let step = 0
    const arm = () => {
      timer = setTimeoutFn(() => {
        timer = null
        if (disposed) return
        if (isSignedIn()) { onRetry(); return }
        step += 1
        if (step < RECHECK_DELAYS_MS.length) { arm(); return }
        onLatch() // fail closed: the token never appeared
      }, RECHECK_DELAYS_MS[step])
    }
    arm()
    return 'recheck'
  }

  return {
    handle401,
    cancel,
    dispose: () => { disposed = true; cancel() },
  }
}

export default createTokenRaceRecovery
