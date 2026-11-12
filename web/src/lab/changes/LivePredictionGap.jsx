import { sentence } from '../honesty.js'
import { practiceMoney, withheld } from '../history/afterSaveHonesty.js'

export default function LivePredictionGap({ comparison }) {
  return <section aria-label="Live versus predicted gap">
    <p>Live versus predicted gap: not measured (no matched comparison receipt)</p>
    {comparison && !withheld(comparison) && <p>Simulated replay · Candidate-minus-Live net delta: {practiceMoney(comparison.deltas?.net_after_fees_cents)} · execution discrepancy. {sentence('replay_is_direction')}</p>}
  </section>
}
