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

import { useCallback, useEffect, useRef, useState } from 'react'

import { applyOverlay } from './overlayTheme.js'
import { decideOverlay, fetchOverlay } from './overlayClient.js'

const EMPTY = { tokens: {}, documentVersion: 0, pendingProposalId: null }

export function useOverlay(sessionId, { enabled = true } = {}) {
  const [state, setState] = useState(EMPTY)
  const [loaded, setLoaded] = useState(false)
  // Monotonic request id: only the NEWEST read may write state. Overlay
  // events arrive in bursts (a remount replays the transcript from seq 0, so
  // every historical overlay event fires a refresh), and concurrent reads can
  // land out of order — an older response would then overwrite a newer one
  // and pin the surface to a stale theme until the next event.
  const readSeqRef = useRef(0)
  const inFlightRef = useRef(false)
  const againRef = useRef(false)

  const reload = useCallback(async () => {
    // fetchOverlay never throws: a theme read is not worth breaking the app,
    // and an empty result leaves the committed defaults exactly as they are.
    const next = await fetchOverlay(sessionId)
    return next
  }, [sessionId])

  useEffect(() => {
    if (!enabled) return undefined
    let alive = true
    const seq = ++readSeqRef.current
    ;(async () => {
      const next = await reload()
      // Two fences: the component may have unmounted (alive), and a coalesced
      // refresh may have superseded this mount read (seq).
      if (!alive || seq !== readSeqRef.current) return
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

  // COALESCED refresh — what a stream event calls. A replay burst of N
  // overlay events must cost ONE settling read, not N concurrent ones: while
  // a read is in flight, further calls only raise a flag, and exactly one
  // more read runs after it lands (so the final state still reflects the last
  // event, never a stale mid-burst snapshot). Out-of-order landings are
  // additionally fenced by the request id.
  const refresh = useCallback(async () => {
    if (inFlightRef.current) { againRef.current = true; return }
    inFlightRef.current = true
    try {
      do {
        againRef.current = false
        const seq = ++readSeqRef.current
        const next = await fetchOverlay(sessionId)
        if (seq !== readSeqRef.current) continue  // a newer read superseded us
        setState(next)
        setLoaded(true)
      } while (againRef.current)
    } finally {
      inFlightRef.current = false
    }
  }, [sessionId])

  return {
    tokens: state.tokens,
    documentVersion: state.documentVersion,
    pendingProposalId: state.pendingProposalId,
    loaded,
    decide,
    reload: refresh,
  }
}
