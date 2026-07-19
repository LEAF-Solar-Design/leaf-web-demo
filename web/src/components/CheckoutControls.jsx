import CheckoutChip from './CheckoutChip.jsx'

// Take / Release affordances over the single-writer checkout (3B). Wave 2 landed
// the lock as a DISPLAY-ONLY chip; this makes it mutable. Three states:
//   - lockedByOther — another editor holds it: the amber CheckoutChip, display
//     only. Write-tool Run is already suppressed by the parent; there is nothing
//     to take here, so no action is offered.
//   - heldByUs      — we hold it: a calm "you hold the edit lock" note + Release.
//   - unlocked      — a quiet "Take edit lock" affordance.
// Calm posture throughout — a checkout is an expected coordination state, never
// an error. Live only (the parent gates on !mock).
export default function CheckoutControls({ lockedByOther, heldByUs, busy, onTake, onRelease }) {
  if (lockedByOther) return <CheckoutChip checkout={lockedByOther} />

  if (heldByUs) {
    return (
      <span className="checkout-controls" role="status">
        <span className="checkout-mine">you hold the edit lock</span>
        <button
          className="btn ghost"
          onClick={onRelease}
          disabled={busy}
          title="Release the single-writer lock so another editor can take it"
        >
          Release
        </button>
      </span>
    )
  }

  return (
    <button
      className="btn ghost"
      onClick={onTake}
      disabled={busy}
      title="Take the single-writer edit lock (da/store.py checkout)"
    >
      Take edit lock
    </button>
  )
}
