// Degraded banner (design 20ab544c9f6b): surfaced whenever an envelope carries
// degraded_mode true (§10) — APS_LIVE execution fell back to the local pure-
// python path. Honest, not alarming: the numbers are valid, a cloud pass would
// refine them.
export default function DegradedBanner({ reason }) {
  return (
    <div className="banner" role="status">
      <span className="tag amber">Degraded</span>
      <div>
        <b>Local fallback, not the cloud solver</b> — this result used the local solver because the
        cloud path was unavailable. The numbers are valid; a cloud pass would refine them.
      </div>
      {reason && <span className="route-note">{reason}</span>}
    </div>
  )
}
