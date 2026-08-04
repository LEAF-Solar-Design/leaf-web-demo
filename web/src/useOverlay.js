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

  // ONE sequenced read primitive serves the mount read, stream refreshes and
  // the post-decision read, because three unsequenced paths raced each other
  // (sol-critic PR #439 round 2):
  //   * readSeq — only the NEWEST read may write state. Overlay events arrive
  //     in bursts (a remount replays the transcript from seq 0, so every
  //     historical overlay event fires a refresh), and concurrent reads can
  //     land out of order; an older response would otherwise overwrite a newer
  //     one and pin the surface to a stale theme until the next event.
  //   * generation — bumped on every session change. Coalescing state MUST NOT
  //     span sessions: an in-flight read for the old session would otherwise
  //     re-fetch the OLD session after the switch, take the newest seq, and
  //     fence out the new session's mount read, leaving the previous tenant's
  //     overlay applied.
  const readSeqRef = useRef(0)
  const genRef = useRef(0)
  const sessionRef = useRef(sessionId)
  // `enabled` is read through a ref, not the closure, because a decision that
  // resolves AFTER the surface went to mock mode still runs its refresh — it
  // captured a live-mode callback and takes the post-disable generation, so
  // neither the generation nor the session fence stops it, and tenant styling
  // lands on the mock surface (sol-critic PR #439 round 6).
  const enabledRef = useRef(enabled)
  const inFlightGenRef = useRef(null)
  const againRef = useRef(false)

  useEffect(() => {
    // A new session invalidates every in-flight read and the coalescing
    // coordinator with them.
    sessionRef.current = sessionId
    genRef.current += 1
    againRef.current = false
    inFlightGenRef.current = null
    setState(EMPTY)
    setLoaded(false)
  }, [sessionId])

  const runRead = useCallback(async () => {
    if (!enabledRef.current) return false
    // Bound to the session THIS closure reads, not to whatever generation is
    // current when it happens to run: a callback captured on session A (a
    // decide() started before a switch) would otherwise stamp itself with B's
    // generation, fetch A, pass both fences, and replace B's state with A's
    // (sol-critic PR #439 round 3).
    if (sessionId !== sessionRef.current) return false
    const gen = genRef.current
    const seq = ++readSeqRef.current
    // fetchOverlay never throws: a theme read is not worth breaking the app,
    // and an empty result leaves the committed defaults exactly as they are.
    const next = await fetchOverlay(sessionId)
    if (!enabledRef.current) return false               // disabled mid-flight
    if (sessionId !== sessionRef.current) return false  // switched mid-flight
    if (gen !== genRef.current) return false      // the session moved on
    if (seq !== readSeqRef.current) return false  // a newer read superseded us
    setState(next)
    setLoaded(true)
    return true
  }, [sessionId])

  // COALESCED refresh — what a stream event and a decision both call. A replay
  // burst of N overlay events must cost ONE settling read, not N concurrent
  // ones: while a read is in flight FOR THIS GENERATION, further calls only
  // raise a flag, and exactly one more read runs after it lands (so the final
  // state still reflects the last event, never a stale mid-burst snapshot).
  //
  // The three resets below (session effect, loop guard, per-iteration flag
  // clear) are MUTUALLY REDUNDANT for the interleavings a test can stage —
  // removing any one, or two, still leaves the observable property intact,
  // which is why the regression test pins the behaviour rather than any single
  // guard. They are each kept because they close different interleavings: the
  // session effect covers an old read waking before the new effect runs, the
  // loop guard covers it waking after.
  const refresh = useCallback(async () => {
    const gen = genRef.current
    if (inFlightGenRef.current === gen) { againRef.current = true; return }
    inFlightGenRef.current = gen
    try {
      do {
        againRef.current = false
        await runRead()
        if (gen !== genRef.current) return   // session changed mid-flight
      } while (againRef.current)
    } finally {
      if (inFlightGenRef.current === gen) inFlightGenRef.current = null
    }
  }, [runRead])

  useEffect(() => {
    enabledRef.current = enabled
    if (!enabled) {
      // Disabling (live -> mock) must FENCE reads already in flight and drop
      // what is applied. The rewrite of this read path lost the original
      // `alive` cleanup, so a live-mode response could land after the switch
      // and paint tenant styling onto the mock surface (sol-critic PR #439
      // round 4). Bumping the generation is the fence; clearing state is what
      // takes the overlay off the screen (the apply effect's cleanup runs on
      // loaded going false).
      genRef.current += 1
      againRef.current = false
      inFlightGenRef.current = null
      setState(EMPTY)
      setLoaded(false)
      return undefined
    }
    void refresh()
    // Unmount, or any change to this effect's inputs, invalidates whatever is
    // in flight — the same fence by the same mechanism.
    return () => { genRef.current += 1 }
  }, [enabled, refresh])

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
    // way to notice; the server is the only thing that knows the truth. Routed
    // through the SAME sequenced coordinator: an unsequenced write here could
    // land after a newer event-driven read and overwrite it with older state.
    await refresh()
    return result
  }, [refresh])

  return {
    tokens: state.tokens,
    documentVersion: state.documentVersion,
    pendingProposalId: state.pendingProposalId,
    loaded,
    decide,
    reload: refresh,
  }
}
