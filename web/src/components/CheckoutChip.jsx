import './panels.css'
import { absoluteWithZone, relativeUntil } from '../lib/railTime.js'
import useRelativeNow from '../lib/useRelativeNow.js'

// Single-writer checkout line: shown near the version note when the drawing's
// version manifest carries a non-null `checkout` lock held by SOMEONE ELSE
// (GET /api/drawings/{id}/versions -> checkout:{holder,acquired,expires}). Calm
// amber advisory posture (square dot + word — an expected coordination state,
// not an error). While it shows, Run is suppressed for write tools (read tools
// are unaffected) — the parent enforces that; this is THE one honest surface
// for the condition (the per-tool copies were consolidated here).
//
// TM1: the expiry renders as a relative horizon under a day ("until ~40 m"),
// a date after; the absolute clock lives in the hover title.
//
// A LAPSED lease renders no horizon at all. Subtracting `now` from a past
// `expires` used to produce "until ~-4 h" — an interval running backwards, which
// reads as a broken clock rather than as the elapsed lease it is. The elapsed
// case is worded once, by the sibling note in CheckoutControls ("this lease
// looks expired"), next to the Take that clears it; duplicating it here would
// say it twice.
//
// The routine source of that state is GONE: `_checkout_view` now answers null
// for a lease the SERVER considers elapsed, so a holder who closed the tab
// without releasing no longer sits on this read for hours. What remains is the
// case this branch was always for — the server calls the lease live and OUR
// clock disagrees (skew), or the record carries an `expires` we cannot read. The
// browser clock never decides the lock is free (checkoutIdentity.js
// `looksStale`), so the chip must keep rendering sanely when the two disagree.
export default function CheckoutChip({ checkout }) {
  const now = useRelativeNow()
  if (!checkout || !checkout.holder) return null
  const expires = Date.parse(checkout.expires)
  const until = relativeUntil(expires, now)
  return (
    <span className="checkout-chip" role="status" title={absoluteWithZone(expires)}>
      Editing locked by <b>{checkout.holder}</b>
      {until ? <> until <b className="t-rel">{until}</b></> : null}
      {/* No leading punctuation: .checkout-chip is inline-flex with a 6px gap
          (styles.css), so this note is its own flex item and a leading "." or
          "," renders as a mark floating clear of the value before it. */}
      <span className="dim">Read tools still run</span>
    </span>
  )
}
