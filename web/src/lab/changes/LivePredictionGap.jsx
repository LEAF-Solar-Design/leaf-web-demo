import { formatMoney } from '../widgets/widgetResolve.js'

export default function LivePredictionGap({ comparison }) {
  const delta = comparison?.deltas?.net_after_fees_cents
  let formatted = 'Not reported'
  try { formatted = formatMoney(delta == null ? null : String(delta)) } catch { /* Keep malformed figures unavailable. */ }
  return <section aria-label="Live versus predicted gap">
    <p>Live versus predicted gap: not measured (no matched comparison receipt)</p>
    {comparison && <p>Simulated replay, not a verdict · Candidate-minus-Live net delta: {formatted}</p>}
  </section>
}
