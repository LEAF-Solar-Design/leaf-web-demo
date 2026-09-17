import { useRef } from 'react'
import { deriveIosState, humanizeStage, IOS_STATE_LABEL, ShipStage } from './IosSurface.jsx'
import { formatElementId } from '../lib/elementIdentity.js'
import { NO_OCCLUDERS, shortId, useGroundWindow, windowStyle } from '../site/groundWindow.js'

// ---------------------------------------------------------------------------
// iOS: the device stage. A phone silhouette whose screen carries the ship
// lane's state, derived by the SAME function IosSurface uses from the D-1
// contract (leaf.ios-ship-surface.v1); the three lane rungs are the real
// preconditions in order (a revision, mounted readiness, a launchable
// build) and light only from booleans the contract or the console holds.
// ---------------------------------------------------------------------------
export function DeviceGround({
  active = false, enabled = false, contract = null, projectLabel = null, revision = null,
  leavingGround = null, studioShell = false, occluders = NO_OCCLUDERS,
  ship, onLaunch,
}) {
  const boardRef = useRef(null)
  const state = enabled ? (deriveIosState(contract) ?? 'malformed') : 'dormant'
  const leaving = leavingGround === 'device-stage'
  const win = useGroundWindow(active || leaving, studioShell, boardRef, occluders)
  const label = state === 'dormant'
    ? 'Not available yet'
    : state === 'malformed' ? 'Status unreadable' : IOS_STATE_LABEL[state]
  const stage = state === 'in-progress' ? humanizeStage(contract?.build_stage) : null
  const rungs = [
    { id: 'revision', label: 'Approved revision', lit: Boolean(revision) },
    { id: 'readiness', label: 'Mounted Apple readiness', lit: state === 'ready' || state === 'in-progress' },
    { id: 'build', label: 'TestFlight build', lit: state === 'ready' },
  ]
  return (
    <div
      className="studio-ground-device"
      ref={boardRef}
      data-ground="ios"
      data-studio-shell={studioShell ? 'cockpit' : undefined}
      data-state={ship ? ship.phase : state}
      hidden={!active && !leaving}
      data-ground-phase={leaving ? 'leaving' : active && leavingGround ? 'entering' : undefined}
      aria-hidden={leaving ? 'true' : undefined}
      inert={leaving ? '' : undefined}
      role="region"
      aria-label="iOS ship lane"
    >
      <div className="ground-device-stage" style={windowStyle(win)} data-measured={win ? 'true' : 'false'}>
        <div className="device-frame">
          <span className="device-notch" aria-hidden="true" />
          <div className="device-screen">
            <span className="device-k">{ship ? 'Build and delivery status' : 'TestFlight lane'}</span>
            <strong className="device-v" data-testid="device-state">{ship ? ship.phase : label}</strong>
            {(ship ? ship.execution?.failed_stage || ship.execution?.stage : stage) && <span className="device-stage">{ship ? ship.execution?.failed_stage || ship.execution?.stage : stage}</span>}
            {(projectLabel || revision) && (
              <span className="device-meta">
                {projectLabel || 'project'}{revision ? ` · ${shortId(revision)}` : ''}
              </span>
            )}
          </div>
        </div>
        <div className="ground-device-side">
          {ship ? <ShipStage ship={ship} onLaunch={onLaunch} /> : <>
          <ol className="ground-lane" aria-label="Ship lane">
            {rungs.map((rung) => (
              <li key={rung.id} data-rung={rung.id} data-lit={rung.lit ? 'true' : 'false'} data-element-id={formatElementId('rung', rung.id) || undefined}>
                <span className={`dot ${rung.lit ? 'live' : 'hollow'}`} aria-hidden="true" />
                {rung.label}
              </li>
            ))}
          </ol>
          {/* Receipt identity is readiness detail: never shown while dormant. */}
          {state !== 'dormant' && contract?.receipt_id && (
            <p className="ground-note">
              receipt {shortId(contract.receipt_id, 12)}{contract.reported_at ? ` · ${contract.reported_at}` : ''}
            </p>
          )}
          {state === 'dormant' && <p className="ground-note">iOS setup status isn’t available yet.</p>}
          {state === 'never-configured' && <p className="ground-note">No ship-lane readiness has been published for this revision.</p>}
          </>}
        </div>
      </div>
    </div>
  )
}
