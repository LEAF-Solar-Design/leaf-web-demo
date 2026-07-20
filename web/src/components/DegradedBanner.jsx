// Degraded banner: surfaced whenever an envelope carries degraded_mode true
// (§10) — APS_LIVE execution fell back to the local pure-python path. Honest,
// not alarming: the numbers are valid, a cloud pass would refine them.
//
// NT2 ongoing-condition anatomy, one line: the container's own amber square
// dot (.banner::before) + tinted lead word (styles.css `.banner b`) + muted
// sentence + one quiet Details action (reveals the mono reason inline) + a
// self-clearing note. Persists while the condition is true and clears itself —
// never user-dismissed.
import { useState } from 'react'

export default function DegradedBanner({ reason }) {
  const [showReason, setShowReason] = useState(false)
  return (
    <div className="banner" role="status">
      <b>Degraded</b>
      <span className="banner-rest">
        {' — this result used the local solver; the cloud path was unavailable. '}
        The numbers are valid; a cloud pass would refine them.
      </span>
      <span className="banner-tail">
        {reason && (showReason
          ? <span className="banner-reason">{reason}</span>
          : <button className="chip-act" onClick={() => setShowReason(true)}>Details</button>)}
        <span className="banner-since">clears on the next cloud run</span>
      </span>
    </div>
  )
}
