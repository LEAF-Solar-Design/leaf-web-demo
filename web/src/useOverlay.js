/**
 * T1 overlay hook: read on mount, apply to the DOM, expose the pending
 * proposal so a surface can render the operator card.
 *
 * WHY THE APPLY IS AN EFFECT WITH A CLEANUP. `applyOverlay` returns an undo
 * that removes exactly the properties it set. Returning that undo as the
 * effect's cleanup means React removes the previous overlay before applying
 * the next one, and on unmount the committed CSS defaults come back on their
 * own. Any other arrangement leaks custom properties onto :root that outlive
 * the component that set them.
 *
 * WHY A LIFECYCLE FENCE ON THE FETCH. An unmounted component that still
 * applies its result would paint a theme for a session the user has left. The
 * `alive` flag is checked after every await, which is the same fence the
 * converse restore probe needed after a review found it missing.
 */

import { useCallback, useEffect, useState } from 'react'

import { applyOverlay } from './overlayTheme.js'
import { decideOverlay, fetchOverlay } from './overlayClient.js'

const EMPTY = { tokens: {}, documentVersion: 0, pendingProposalId: null }

export function useOverlay(sessionId, { enabled = true } = {}) {
  const [state, setState] = useState(EMPTY)
  const [loaded, setLoaded] = useState(false)

  const reload = useCallback(async () => {
    // fetchOverlay never throws: a theme read is not worth breaking the app,
    // and an empty result leaves the committed defaults exactly as they are.
    const next = await fetchOverlay(sessionId)
    return next
  }, [sessionId])

  useEffect(() => {
    if (!enabled) return undefined
    let alive = true
    ;(async () => {
      const next = await reload()
      if (!alive) return          // the session was left mid-flight
      setState(next)
      setLoaded(true)
    })()
    return () => { alive = false }
  }, [enabled, reload])

  // Applying is its own effect so a re-read swaps the overlay through the same
  // undo path rather than stacking properties on top of the previous set.
  useEffect(() => {
    if (!loaded) return undefined
    return applyOverlay(state.tokens)
  }, [loaded, state.tokens])

  const decide = useCallback(async (proposalId, opts) => {
    const result = await decideOverlay(proposalId, opts)
    // Re-read rather than patching locally from the response. A client that
    // patches its own copy and misses one update stays wrong forever with no
    // way to notice; the server is the only thing that knows the truth.
    const next = await fetchOverlay(sessionId)
    setState(next)
    return result
  }, [sessionId])

  return {
    tokens: state.tokens,
    documentVersion: state.documentVersion,
    pendingProposalId: state.pendingProposalId,
    loaded,
    decide,
    reload: async () => setState(await reload()),
  }
}
