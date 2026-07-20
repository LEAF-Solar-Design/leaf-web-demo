import CheckoutChip from './CheckoutChip.jsx'

// Take / Release affordances over the single-writer checkout (3B). Wave 2 landed
// the lock as a DISPLAY-ONLY chip; this makes it mutable. Three states:
//   - lockedByOther — another editor holds it: the amber square-dot CheckoutChip
//     line, display only. Write-tool Run is already suppressed by the parent;
//     there is nothing to take here, so no action is offered.
//   - heldByUs      — we hold it: a green dot "You hold the edit lock" + Release.
//   - unlocked      — a quiet "Take edit lock" chip.
// Calm posture throughout — a checkout is an expected coordination state, never
// an error. Live only (the parent gates on !mock).
export default function CheckoutControls({ lockedByOther, heldByUs, busy, onTake, onRelease }) {
  if (lockedByOther) return <CheckoutChip checkout={lockedByOther} />

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
