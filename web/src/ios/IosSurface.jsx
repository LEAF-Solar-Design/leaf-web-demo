// IosSurface — renders iOS setup readiness strictly from the D-1 ship-lane
// contract (leaf.ios-ship-surface.v1, validated server-side in
// server/routers/ios_surface.py). Purely props-driven (EntitlementGate.jsx /
// Membership.jsx pattern): no fetch, no polling, no client-side state math.
//
// The contract carries EXACTLY: readiness = { healthy: bool, launchable:
// bool }, build_stage (a word from the published 14-stage vocabulary, or
// null), receipt_id, reported_at. The four views are pure derivations:
//
//   contract absent (null)        -> 'never-configured' (nothing published)
//   readiness.healthy === false   -> 'unavailable'
//   healthy && launchable         -> 'ready'
//   healthy && !launchable        -> 'in-progress' (signal = build_stage)
//
// No state may invent progress the contract didn't report: the contract has
// NO percentage field, so 'in-progress' shows the build_stage word and
// nothing else — never a number, never an indeterminate bar. A contract
// whose readiness is missing either boolean is malformed: render nothing
// rather than guess (the server's validator fails such contracts closed, so
// this is a defensive fence, not a real state).
//
// While the ios_surface flag is off, the surface stays dormant: a neutral
// placeholder revealing no readiness detail at all.

const STATE_LABEL = {
  ready: 'Ready',
  'in-progress': 'Setting up',
  unavailable: 'Unavailable',
  'never-configured': 'Not yet configured',
}

// "MAC_ALLOCATED" -> "Mac allocated": echo the contract's own stage word in
// readable form; never a synthesized fraction of done-ness.
function humanizeStage(stage) {
  if (typeof stage !== 'string' || !stage) return null
  const words = stage.toLowerCase().split('_').join(' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

function deriveState(contract) {
  if (contract == null) return 'never-configured'
  const readiness = contract.readiness
  if (!readiness
    || typeof readiness.healthy !== 'boolean'
    || typeof readiness.launchable !== 'boolean') return null // malformed: no guess
  if (!readiness.healthy) return 'unavailable'
  return readiness.launchable ? 'ready' : 'in-progress'
}

export default function IosSurface({ enabled, contract }) {
  if (!enabled) {
    return (
      <section className="ios-surface ios-surface-dormant" aria-label="iOS readiness" data-state="dormant">
        <p className="ios-dormant-note dim">iOS setup status isn’t available yet.</p>
      </section>
    )
  }

  const state = deriveState(contract)
  if (state === null) return null

  if (state === 'in-progress') {
    const stageLabel = humanizeStage(contract.build_stage)
    return (
      <section className="ios-surface ios-surface-in-progress" aria-label="iOS readiness" data-state="in-progress" role="status">
        <span className="ios-state-k">iOS app</span>
        <span className="ios-state-v ios-in-progress">
          {STATE_LABEL['in-progress']}
          {stageLabel ? ` — ${stageLabel}` : ''}
        </span>
      </section>
    )
  }

  if (state === 'ready') {
    return (
      <section className="ios-surface ios-surface-ready" aria-label="iOS readiness" data-state="ready">
        <span className="ios-state-k">iOS app</span>
        <span className="ios-state-v ios-ready">{STATE_LABEL.ready}</span>
      </section>
    )
  }

  if (state === 'unavailable') {
    return (
      <section className="ios-surface ios-surface-unavailable" aria-label="iOS readiness" data-state="unavailable" role="alert">
        <span className="ios-state-k">iOS app</span>
        <span className="ios-state-v ios-unavailable">{STATE_LABEL.unavailable}</span>
      </section>
    )
  }

  // state === 'never-configured': no contract has ever been published for
  // this project/revision — say so, fabricate nothing.
  return (
    <section className="ios-surface ios-surface-never-configured" aria-label="iOS readiness" data-state="never-configured">
      <span className="ios-state-k">iOS app</span>
      <span className="ios-state-v ios-never-configured">{STATE_LABEL['never-configured']}</span>
    </section>
  )
}

// Shared with the studio shell's device ground (site/SurfaceGrounds.jsx):
// ONE derivation of the ship-lane state from the contract, never two.
export { deriveState as deriveIosState, humanizeStage, STATE_LABEL as IOS_STATE_LABEL }
