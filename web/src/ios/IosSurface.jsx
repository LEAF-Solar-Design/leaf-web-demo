// IosSurface — renders the iOS app's setup readiness strictly from the
// server's readiness contract. Purely props-driven (EntitlementGate.jsx /
// Membership.jsx pattern): this component owns no fetch, no polling, no
// client-side state math — `readiness` is the server's own answer to "what
// state is iOS setup actually in", and every branch below only echoes it.
//
// Four contract states, four DISTINCT truthful views: 'ready',
// 'in-progress', 'unavailable', 'never-configured'. None of them may invent
// progress the contract didn't report — in particular, 'in-progress' shows a
// percentage ONLY when readiness.progress is a real number from the
// contract, and never an animated/indeterminate stand-in when it is absent.
//
// While the ios_surface flag is off, the surface stays dormant: a neutral
// placeholder that reveals no readiness detail at all (there is nothing to
// show for a feature that is not rolled out).

const KNOWN_STATES = ['ready', 'in-progress', 'unavailable', 'never-configured']

const STATE_LABEL = {
  ready: 'Ready',
  'in-progress': 'Setting up',
  unavailable: 'Unavailable',
  'never-configured': 'Not yet configured',
}

function ReadinessDetail({ detail }) {
  if (!detail) return null
  return <p className="ios-detail">{detail}</p>
}

export default function IosSurface({ enabled, readiness }) {
  if (!enabled) {
    return (
      <section className="ios-surface ios-surface-dormant" aria-label="iOS readiness" data-state="dormant">
        <p className="ios-dormant-note dim">iOS setup status isn’t available yet.</p>
      </section>
    )
  }

  const state = readiness?.state
  const detail = readiness?.detail || null

  // No contract read yet, or a state this view doesn't recognize — render
  // nothing rather than guess at a status the server never reported.
  if (!KNOWN_STATES.includes(state)) return null

  if (state === 'in-progress') {
    // Only a real, contract-supplied number is ever shown. A missing
    // `progress` renders the plain "Setting up" label with no percentage
    // and no fake/indeterminate bar — that is the whole "no fabrication"
    // requirement for this state.
    const hasProgress = typeof readiness.progress === 'number' && Number.isFinite(readiness.progress)
    return (
      <section className="ios-surface ios-surface-in-progress" aria-label="iOS readiness" data-state="in-progress" role="status">
        <span className="ios-state-k">iOS app</span>
        <span className="ios-state-v ios-in-progress">
          {STATE_LABEL['in-progress']}
          {hasProgress ? ` — ${Math.round(readiness.progress)}% complete` : ''}
        </span>
        <ReadinessDetail detail={detail} />
      </section>
    )
  }

  if (state === 'ready') {
    return (
      <section className="ios-surface ios-surface-ready" aria-label="iOS readiness" data-state="ready">
        <span className="ios-state-k">iOS app</span>
        <span className="ios-state-v ios-ready">{STATE_LABEL.ready}</span>
        <ReadinessDetail detail={detail} />
      </section>
    )
  }

  if (state === 'unavailable') {
    return (
      <section className="ios-surface ios-surface-unavailable" aria-label="iOS readiness" data-state="unavailable" role="alert">
        <span className="ios-state-k">iOS app</span>
        <span className="ios-state-v ios-unavailable">{STATE_LABEL.unavailable}</span>
        <ReadinessDetail detail={detail} />
      </section>
    )
  }

  // state === 'never-configured'
  return (
    <section className="ios-surface ios-surface-never-configured" aria-label="iOS readiness" data-state="never-configured">
      <span className="ios-state-k">iOS app</span>
      <span className="ios-state-v ios-never-configured">{STATE_LABEL['never-configured']}</span>
      <ReadinessDetail detail={detail} />
    </section>
  )
}
