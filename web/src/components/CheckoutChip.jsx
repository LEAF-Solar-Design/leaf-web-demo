import './panels.css'

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
// A LAPSED lease renders no horizon at all. The server publishes the checkout
// record verbatim, expired or not (routers/drawings.py `_checkout_view`), so a
// holder who closed the tab without releasing stays on this read until someone
// takes the lock. Subtracting `now` from a past `expires` used to produce
// "until ~-4 h" — an interval running backwards, which reads as a broken clock
// rather than as the elapsed lease it is. The elapsed case is worded once, by
// the sibling note in CheckoutControls ("this lease looks expired"), next to
// the Take that clears it; duplicating it here would say it twice.
function fmtUntil(iso) {
  if (!iso) return null
  const d = new Date(iso)
  // An `expires` we cannot parse is not a horizon either, and echoing it raw
  // put the string itself where a duration goes ("until banana"). It is a real
  // record shape, not a hypothetical: `looksStale` already treats an
  // unparseable expiry as elapsed for exactly this reason, so the rail said
  // "until banana … this lease looks expired" in one breath. Same answer as the
  // elapsed case — no horizon, and let the sibling note carry the meaning.
  if (Number.isNaN(d.getTime())) return { rel: null, abs: null }
  const abs = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  const remainingMs = d.getTime() - Date.now()
  // At or past expiry there is no horizon left to name: a null `rel` is what
  // tells the caller to drop the "until …" clause, and the absolute clock stays
  // for the hover title. The test is on the raw millisecond delta, not on
  // rounded minutes, so a lease with 20 seconds left rounds UP to "~1 m" rather
  // than down into the expired branch.
  if (remainingMs <= 0) return { rel: null, abs }
  const mins = Math.max(Math.round(remainingMs / 60000), 1)
  if (mins >= 1440) {
    return { rel: d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }), abs }
  }
  if (mins >= 60) return { rel: `~${Math.round(mins / 60)} h`, abs }
  return { rel: `~${mins} m`, abs }
}

export default function CheckoutChip({ checkout }) {
  if (!checkout || !checkout.holder) return null
  const until = fmtUntil(checkout.expires)
  return (
    <span className="checkout-chip" role="status" title={until?.abs || undefined}>
      Editing locked by <b>{checkout.holder}</b>
      {until?.rel ? <> until <b className="t-rel">{until.rel}</b></> : null}
      <span className="dim"> — read tools still run</span>
    </span>
  )
}
