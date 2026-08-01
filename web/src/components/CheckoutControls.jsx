import CheckoutChip from './CheckoutChip.jsx'

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
export default function CheckoutControls({
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
          Could not read the edit lock — writes paused
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
