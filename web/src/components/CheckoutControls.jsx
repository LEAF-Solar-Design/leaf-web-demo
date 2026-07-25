import CheckoutChip from './CheckoutChip.jsx'

// Take / Release affordances over the single-writer checkout (3B). Wave 2 landed
// the lock as a DISPLAY-ONLY chip; this makes it mutable. States:
//   - unknown       — the lock read FAILED, so we do not know who holds it. The
//     parent fails closed and suppresses write-Run, and this line is what stops
//     that from being a silent disable: the user is told we could not read the
//     lock and offered a retry, rather than finding Run mysteriously inert.
//   - lockedByOther — another editor holds it: the amber square-dot CheckoutChip
//     line, display only. Write-tool Run is already suppressed by the parent;
//     there is nothing to take here, so no action is offered.
//   - legacyByOther — held by a pre-session-id client (a tenant-shaped holder).
//     Same suppression, but named, because "held by acme" with no Release button
//     is otherwise indistinguishable from a bug. The server caps every lease at
//     24h and frees expired locks, so these drain on their own.
//   - heldByUs      — we hold it: a green dot "You hold the edit lock" + Release.
//   - unlocked      — a quiet "Take edit lock" chip.
// Calm posture throughout — a checkout is an expected coordination state, never
// an error. Live only (the parent gates on !mock).
export default function CheckoutControls({
  lockedByOther, legacyByOther, staleByOther, canTake, heldByUs, unknown, busy, onTake, onRelease, onRetry,
}) {
  if (unknown) {
    return (
      <span className="checkout-controls" role="status">
        <span className="checkout-unknown">
          Could not read the edit lock — writes paused
        </span>
        {onRetry && (
          <button className="chip-act" onClick={onRetry} disabled={busy}>
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
          <button className="chip-act" onClick={onTake} disabled={busy}>
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
        <button className="chip-act" onClick={onRelease} disabled={busy}>
          Release
        </button>
      </span>
    )
  }

  return (
    <button className="chip-act" onClick={onTake} disabled={busy}>
      Take edit lock
    </button>
  )
}
