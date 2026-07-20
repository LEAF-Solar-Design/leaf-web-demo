// Quota banner: a run rejected by a per-tenant limit. Two distinct kinds, both
// calm amber (an expected budget/allowance state, never a red failure):
//
//   default        — the broker's hard pre-flight SPEND cap (HTTP 402). Keyed on
//                     dollars remaining; resolves when spend falls back under cap.
//   kind:daily_runs — the coarse DAILY RUN-COUNT limit (HTTP 429). Keyed on
//                     used/limit for the tier; resets at 00:00 UTC.
//
// NT2 ongoing-condition anatomy, one line: the container's own amber square
// dot (.banner::before) + tinted lead word (styles.css `.banner b`) + muted
// sentence + at most one quiet action (rendered only when the parent wires
// `onAction`, e.g. scroll-to-usage) + a self-clearing note. Persists while the
// condition is true and clears itself — never user-dismissed. Nothing ran on
// the cloud in either case, so the run was never charged; the backend's own
// message is authoritative.
export default function QuotaCard({ kind, message, remaining, tier, limit, used, onAction }) {
  if (kind === 'daily_runs') {
    const haveCounts = Number.isFinite(Number(used)) && Number.isFinite(Number(limit))
    return (
      <div className="banner quota" role="status">
        <b>Daily limit</b>
        <span className="banner-rest">
          {' — '}
          {haveCounts ? `${used}/${limit} runs used` : 'daily run limit reached'}
          {tier ? ` on the ${tier} tier` : ''}
          {'; '}
          {message || 'this run was rejected before any cloud work — nothing was charged.'}
        </span>
        <span className="banner-tail">
          {onAction && <button className="chip-act" onClick={onAction}>View usage</button>}
          <span className="banner-since">resets at 00:00 UTC · clears itself</span>
        </span>
      </div>
    )
  }
  const hasRemaining = typeof remaining === 'number' && Number.isFinite(remaining)
  return (
    <div className="banner quota" role="status">
      <b>Spend cap</b>
      <span className="banner-rest">
        {' — this run wasn’t charged; '}
        {message || 'nothing ran on the cloud — the run was rejected before any billable work.'}
        {hasRemaining ? ` $${remaining.toFixed(2)} left under the cap.` : ''}
      </span>
      <span className="banner-tail">
        {onAction && <button className="chip-act" onClick={onAction}>View usage</button>}
        <span className="banner-since">clears when spend falls under the cap</span>
      </span>
    </div>
  )
}
