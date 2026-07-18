// Single-writer checkout chip: shown near the version note when the drawing's
// version manifest carries a non-null `checkout` lock held by SOMEONE ELSE
// (GET /api/drawings/{id}/versions -> checkout:{holder,acquired,expires}). Calm
// amber posture (this is an expected coordination state, not an error). While
// it shows, Run is suppressed for write tools (read tools are unaffected) — the
// parent enforces that; this chip is the honest surface for it.
function fmtWhen(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export default function CheckoutChip({ checkout }) {
  if (!checkout || !checkout.holder) return null
  const until = fmtWhen(checkout.expires)
  return (
    <span className="checkout-chip" role="status" title="Single-writer lock (da/store.py checkout)">
      editing locked by <b>{checkout.holder}</b>{until ? <> until <b>{until}</b></> : null}
    </span>
  )
}
