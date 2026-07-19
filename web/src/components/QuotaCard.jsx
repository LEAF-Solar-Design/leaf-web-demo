// Quota card: a run rejected by a per-tenant limit. Two distinct kinds, both
// calm amber (an expected budget/allowance state, never a red failure):
//
//   default        — the broker's hard pre-flight SPEND cap (HTTP 402). Keyed on
//                     dollars remaining; resolves when spend falls back under cap.
//   kind:daily_runs — the coarse DAILY RUN-COUNT limit (HTTP 429). Keyed on
//                     used/limit for the tier; resets at 00:00 UTC.
//
// Nothing ran on the cloud in either case, so the run was never charged. The
// backend's own message is authoritative; a soft second line says what unblocks
// new runs. Rendered only for the quota case (never for generic failures).
export default function QuotaCard({ kind, message, remaining, tier, limit, used }) {
  if (kind === 'daily_runs') {
    const haveCounts = Number.isFinite(Number(used)) && Number.isFinite(Number(limit))
    return (
      <div className="banner quota" role="status">
        <span className="tag amber">Daily limit</span>
        <div>
          <b>
            Daily run limit reached{haveCounts ? ` — ${used}/${limit} runs` : ''}
            {tier ? ` on the ${tier} tier` : ''}.
          </b>{' '}
          {message || 'This run was rejected before any cloud work — nothing was charged.'}
          <div className="quota-sub">
            Your daily run allowance resets at <b>00:00 UTC</b>. Runs resume automatically then,
            or sooner if your plan’s daily limit is raised. No cloud work was billed for this attempt.
          </div>
        </div>
      </div>
    )
  }
  const hasRemaining = typeof remaining === 'number' && Number.isFinite(remaining)
  return (
    <div className="banner quota" role="status">
      <span className="tag amber">Spend cap</span>
      <div>
        <b>Spend cap reached — this run wasn’t charged.</b>{' '}
        {message || 'Nothing ran on the cloud; the run was rejected before any billable work.'}
        <div className="quota-sub">
          New runs resume once your spend falls back under the cap
          {hasRemaining ? ` (currently $${remaining.toFixed(2)} left)` : ''}, or when your
          plan’s limit is raised. No cloud work was billed for this attempt.
        </div>
      </div>
    </div>
  )
}
