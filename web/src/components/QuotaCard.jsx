// Quota card: a run rejected by the broker's hard pre-flight spend cap
// (error_code === 'quota_exceeded', HTTP 402). Reuses the DegradedBanner shape
// with a calm amber posture — this is an expected budget state, not a failure to
// alarm about. Nothing ran on the cloud, so the run was never charged. The
// backend's own message is authoritative; a soft second line says what unblocks
// new runs. Rendered only for the quota case (never for generic failures).
export default function QuotaCard({ message, remaining }) {
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
