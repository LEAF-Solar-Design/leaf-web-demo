import CheckoutChip from './CheckoutChip.jsx'
import { useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { humanizeError } from '../errorHumanize.js'
import { useDrawingIdentityOptional } from '../drawing/DrawingIdentityProvider.jsx'
import { authTenantScope } from '../drawing/drawingIdentity.js'
import { subscribeTokenStored } from '../auth.js'
import { subscribeUnauthorized } from '../api.js'

function readAccountScope() {
  try { return authTenantScope(globalThis.localStorage?.getItem('leaf.jwt')) } catch { return null }
}

function subscribeAccountScope(listener) {
  const offStored = subscribeTokenStored(listener)
  const offUnauthorized = subscribeUnauthorized(listener)
  const onStorage = (event) => {
    if (event.key == null || event.key === 'leaf.jwt') listener()
  }
  globalThis.addEventListener?.('storage', onStorage)
  return () => {
    offStored?.()
    offUnauthorized?.()
    globalThis.removeEventListener?.('storage', onStorage)
  }
}

export default function CheckoutControls(props) {
  const identity = useDrawingIdentityOptional()
  const drawingKey = props.drawingKey ?? props.drawingId ?? identity?.drawingId
  const accountScope = useSyncExternalStore(subscribeAccountScope, readAccountScope, readAccountScope)
  const [error, setError] = useState(null)
  const [pending, setPending] = useState(false)
  const root = useRef(null)
  const restoreFocus = useRef(false)
  const generation = useRef(0)
  useLayoutEffect(() => {
    generation.current += 1
    restoreFocus.current = false
    setError(null)
    setPending(false)
    return () => { generation.current += 1 }
  }, [drawingKey, accountScope])
  useLayoutEffect(() => {
    if (!pending && restoreFocus.current) {
      restoreFocus.current = false
      ;(root.current?.querySelector('button:not(:disabled)') || root.current)?.focus()
    }
  }, [pending])
  const perform = (action, taking = false, reading = false) => async () => {
    const request = ++generation.current
    restoreFocus.current = root.current?.contains(document.activeElement)
    setError(null)
    setPending(true)
    try {
      const result = await action?.()
      if (generation.current !== request) return
      if ((!reading && result === null) || (taking && result?.acquired === false)) {
        setError(humanizeError(taking
          ? 'The edit lock could not be taken. Another tab or editor may be using it. Try again.'
          : 'The edit lock could not be released. Try again.'))
      }
    } catch (cause) {
      if (generation.current === request) setError(humanizeError(cause))
    } finally {
      if (generation.current === request) setPending(false)
    }
  }
  return <span ref={root} tabIndex={-1} className="checkout-controls">
    <CheckoutStateControls {...props} busy={props.busy || pending}
      onTake={perform(props.onTake, true)} onRelease={perform(props.onRelease)}
      onRetry={props.onRetry ? perform(props.onRetry, false, true) : undefined} />
    {error && <span role="alert">{error}</span>}
    {(pending || props.busy) && <span role="status">Updating the edit lock…</span>}
    {props.disabled && <span>Open a drawing and sign in to change the edit lock.</span>}
  </span>
}

// Take / Release affordances over the single-writer checkout (3B). Wave 2 landed
// the lock as a DISPLAY-ONLY chip; this makes it mutable. States, in the order
// they are decided:
//   - unknown       — we have no answer for the CURRENT drawing: either a read is
//     in flight or the last one failed. The parent fails closed and suppresses
//     write-Run either way, and this line is what stops that from being a silent
//     disable. `readFailed` picks the wording, because every read now starts by
//     going unknown, so a routine refetch also lands here and "could not read"
//     would be a lie. Only the failed case offers a Retry.
//   - lockedByOther — another editor holds it: the amber square-dot CheckoutChip.
//     A Take is ALWAYS offered here, and deliberately never gated on our clock:
//     the server decides, handing over an elapsed lease or returning 409 for a
//     live one. `staleByOther` and `legacyByOther` only choose the wording, since
//     gating the button on a clock-derived guess is what used to leave a wedge no
//     user action could clear.
//   - heldByUs      — we hold it: a green dot "You hold the edit lock" + Release.
//   - unlocked      — a quiet "Take edit lock" chip.
// Calm posture throughout — a checkout is an expected coordination state, never
// an error. Live only (the parent gates on !mock).
function CheckoutStateControls({
  lockedByOther, legacyByOther, staleByOther, canTake, heldByUs, unknown, readFailed, busy, disabled = false, onTake, onRelease, onRetry,
}) {
  if (unknown) {
    // Both states pause writes, and only the wording differs. Every read now
    // starts by going unknown (so a drawing change cannot leave the previous
    // drawing's answer authoritative), which means a routine refetch lands here
    // too. Saying "could not read" during a refetch that is simply in flight
    // would be false, and a Retry button for a request already running is noise.
    if (!readFailed) {
      return (
        <span className="checkout-controls" role="status">
          <span className="checkout-checking">Checking the edit lock…</span>
        </span>
      )
    }
    return (
      <span className="checkout-controls" role="status">
        <span className="checkout-unknown">
          Could not read the edit lock, so writes are paused
        </span>
        {onRetry && (
          <button className="chip-act" onClick={onRetry} disabled={busy || disabled}>
            Retry
          </button>
        )}
      </span>
    )
  }

  if (lockedByOther) {
    // Someone else holds it. Always offer a Take, and never gate that on our own
    // clock: the server adjudicates, either handing over an elapsed lease or
    // returning 409 for a live one, after which the refetch shows the truth. A
    // rejected take costs one request; hiding the button leaves a wedge that no
    // user action can clear, which is what a skewed clock used to produce.
    // `staleByOther` and `legacyByOther` only choose the wording.
    const note = staleByOther
      ? (legacyByOther ? 'held by an older client, and its lease looks expired' : 'this lease looks expired')
      : (legacyByOther ? 'held by an older client' : null)
    return (
      <span className="checkout-controls" role="status">
        <CheckoutChip checkout={lockedByOther} />
        {note && <span className={staleByOther ? 'checkout-stale' : 'checkout-legacy'}>{note}</span>}
        {canTake && (
          <button className="chip-act" onClick={onTake} disabled={busy || disabled}>
            Take edit lock
          </button>
        )}
      </span>
    )
  }

  if (heldByUs) {
    return (
      <span className="checkout-controls" role="status">
        <span className="checkout-mine">You hold the edit lock</span>
        <button className="chip-act" onClick={onRelease} disabled={busy || disabled}>
          Release
        </button>
      </span>
    )
  }

  return (
    <button className="chip-act" onClick={onTake} disabled={busy || disabled}>
      Take edit lock
    </button>
  )
}
