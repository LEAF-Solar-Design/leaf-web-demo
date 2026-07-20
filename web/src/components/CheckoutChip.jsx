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
function fmtUntil(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return { rel: String(iso), abs: null }
  const abs = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  const mins = Math.round((d.getTime() - Date.now()) / 60000)
  if (Math.abs(mins) >= 1440) {
    return { rel: d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }), abs }
  }
  if (Math.abs(mins) >= 60) return { rel: `~${Math.round(mins / 60)} h`, abs }
  return { rel: `~${Math.max(mins, 1)} m`, abs }
}

export default function CheckoutChip({ checkout }) {
  if (!checkout || !checkout.holder) return null
  const until = fmtUntil(checkout.expires)
  return (
    <span className="checkout-chip" role="status" title={until?.abs || undefined}>
      Editing locked by <b>{checkout.holder}</b>
      {until ? <> until <b className="t-rel">{until.rel}</b></> : null}
      <span className="dim"> — read tools still run</span>
    </span>
  )
}
