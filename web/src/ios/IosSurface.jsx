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

import ShipReceipts from './ShipReceipts.jsx'
import { humanizeError } from '../errorHumanize.js'
import { iosSourceApprovalState } from '../site/iosShipReadiness.js'

const SHIP_APPROVE_REASONS = Object.freeze({
  owner: 'Only the project owner can approve a source revision',
  noRevision: 'Select a drawing version before approving',
  busy: 'The approval is being recorded.',
})

const SHIP_PHASE_REASONS = Object.freeze({
  idle: 'Sign in and select an approved revision to launch.',
  loading: 'Checking whether you can start a build.',
  'setup-required': 'Finish setup before starting a build.',
  launching: 'The build is being submitted.',
  running: 'A build is already running.',
  succeeded: 'This build has succeeded.',
  failed: 'This build has failed.',
  unavailable: 'Build status is unavailable. Try checking again.',
  ready: 'Launch the approved revision.',
})

function setupSentence(readiness) {
  const state = readiness?.setupState
  if (state === 'no-approved-revision') return 'Select a drawing version and ask the project owner to approve it.'
  if (state === 'grant-not-ready') return 'Ask the project owner to connect the Apple account before starting a build.'
  if (state === 'executor-busy') return 'Another build is using the build computer. Check again when it finishes.'
  if (state === 'executor-unavailable') return 'The build computer is unavailable. Ask support to reconnect it, then check again.'
  if (readiness?.setupAction) return 'Ask support to connect the Apple build service, then check again.'
  return null
}

export function ShipStage({ ship, onLaunch }) {
  const { readiness, execution, receipt, phase, error } = ship
  // IOSGUEST: an idle lane (the controller is off: signed out, the demo, or the flag) has no readiness to read, so say
  // what unlocks it instead of a ladder of default "missing" rungs.
  if (phase === 'idle') {
    return (
      <div className="ios-ship-stage" data-state="idle">
        <p role="status">{SHIP_PHASE_REASONS.idle}</p>
      </div>
    )
  }
  const setup = readiness?.setupState
  const launched = Boolean(execution)
  const rungs = [
    ['source', 'Source (approved revision)', setup === 'no-approved-revision' ? 'missing' : readiness?.approvedLaunch || launched ? 'ready' : 'missing'],
    ['grant', 'Apple account', setup === 'grant-not-ready' ? 'missing' : readiness?.grantStatus === 'healthy' || launched ? 'ready' : 'missing'],
    ['executor', 'Build computer', setup === 'executor-busy' ? 'busy' : setup === 'executor-unavailable' ? 'unavailable' : readiness?.dispatchAvailable || launched ? 'ready' : 'unavailable'],
    ['build', 'Build', execution?.status === 'succeeded' ? 'ready' : execution?.status === 'failed' ? 'unavailable' : launched || phase === 'launching' ? 'busy' : 'missing'],
    ['delivery', 'Delivery', receipt ? 'ready' : execution?.status === 'failed' ? 'unavailable' : launched ? 'busy' : 'missing'],
  ]
  return (
    <div className="ios-ship-stage" data-state={phase}>
      <p role="status">{SHIP_PHASE_REASONS[phase] || SHIP_PHASE_REASONS.unavailable}</p>
      <ol className="ground-lane" aria-label="Ship stage ladder" data-testid="ios-stage-ladder">
        {rungs.map(([id, label, state]) => (
          <li key={id} data-rung={id} data-state={state}>{label}: {state}</li>
        ))}
      </ol>
      {setupSentence(readiness) && <p>{setupSentence(readiness)}</p>}
      {Array.isArray(ship.sources) && (
        <section className="ios-ship-sources" data-testid="ios-ship-sources" aria-label="Imported source revisions">
          {ship.sync && ship.sync.status !== 'ok' && <p data-testid="ios-ship-sync">Source updates are unavailable. Showing saved versions. Check again shortly.</p>}
          {ship.sources.length === 0 ? <p>No imported source revision yet.</p> : (
            <ul>
              {ship.sources.map((source) => {
                const state = iosSourceApprovalState(source, ship.approvals || [], ship.revision)
                return (
                  <li key={source.source_revision} data-testid="ios-ship-source" data-revision={source.source_revision} data-state={state}>
                    {source.bundle_identifier} · {source.marketing_version} ({source.build_number}) · {source.source_revision.slice(0, 12)}
                    {state === 'approved' ? <span>approved for the selected version</span>
                      : state === 'consumed' ? <span>approval consumed</span>
                        : ship.canApprove === true && typeof ship.approve === 'function' && ship.revision ? (
                          <button type="button" data-testid="ios-ship-approve" disabled={ship.approving === true}
                            title={ship.approving ? SHIP_APPROVE_REASONS.busy : 'Approve for the selected version'}
                            onClick={() => ship.approve(source.source_revision)}>Approve for the selected version</button>
                        ) : <p>{!ship.revision ? SHIP_APPROVE_REASONS.noRevision : SHIP_APPROVE_REASONS.owner}</p>}
                  </li>
                )
              })}
            </ul>
          )}
          {ship.sourcesError && <p role="alert" data-testid="ios-ship-sources-error">{humanizeError(ship.sourcesError)}</p>}
        </section>
      )}
      {execution && (
        <p className="ios-execution-status">
          {humanizeStage(execution.failed_stage || execution.stage)}
          {execution.updated_at && <time dateTime={execution.updated_at}> {execution.updated_at}</time>}
        </p>
      )}
      {receipt && <ShipReceipts receipts={[receipt]} />}
      {error && <p role="alert">{humanizeError(error)}</p>}
      {ship.followStopped && ship.refresh && <button type="button" onClick={ship.refresh}>Resume status updates</button>}
      {['succeeded', 'failed'].includes(phase) && ship.nextBuild && (
        <button type="button" disabled={ship.loading} onClick={ship.nextBuild}>Check for next build</button>
      )}
      {['setup-required', 'unavailable'].includes(phase) && ship.refresh && (
        <button type="button" disabled={ship.loading} onClick={ship.refresh}>Check build readiness</button>
      )}
      {onLaunch && (
        <>
          <button type="button" disabled={phase !== 'ready'} title={SHIP_PHASE_REASONS[phase]} onClick={onLaunch}>Launch TestFlight build</button>
        </>
      )}
    </div>
  )
}

const STATE_LABEL = {
  ready: 'Ready',
  'in-progress': 'Setting up',
  unavailable: 'Unavailable',
  'never-configured': 'Not yet configured',
}

// Describe only known contract stages; never echo an unknown provider token.
function humanizeStage(stage) {
  if (typeof stage !== 'string' || !stage) return null
  return ({
    SOURCE_APPROVED: 'Source approved', GRANT_READY: 'Apple account connected',
    BUNDLE_READY: 'App files ready', MAC_ALLOCATED: 'Build computer ready',
    APP_RECORD: 'App registered', BUILT: 'App built', UPLOADED: 'App uploaded',
    XCODE_READY: 'Build software ready', SIGNING_READY: 'App signing ready',
    COMPLIANCE: 'Checking app requirements', BETA_ASSIGNED: 'Testers assigned',
    CREDENTIALS_SCRUBBED: 'Apple account details removed', MAC_RELEASED: 'Build computer released',
    RECEIPT: 'Build confirmation ready',
  })[stage] || 'Check the build details for the latest update.'
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

export default function IosSurface({ enabled, contract, ship, onLaunch }) {
  if (!enabled) {
    return (
      <section className="ios-surface ios-surface-dormant" aria-label="iOS readiness" data-state="dormant">
        <p className="ios-dormant-note dim">iOS setup status isn’t available yet.</p>
      </section>
    )
  }

  if (ship) {
    return <section className="ios-surface" aria-label="iOS readiness" data-state={ship.phase}><ShipStage ship={ship} onLaunch={onLaunch} /></section>
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
          {stageLabel ? `: ${stageLabel}` : ''}
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
