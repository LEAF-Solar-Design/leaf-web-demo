// WHAT THIS CAPSULE DOES NOT DO YET (the ledger's 9c row is not claimed by
// the PR that added it): containment checks the scoped delta through an
// injected resolver, not a live manifest check against the repo, and accept
// does not commit the transform into the user's repo through the fold. Both
// are owed; the capsule shows state honestly meanwhile and never reads as
// settled before a receipt says so.
// THE CHANGE CAPSULE (standardization slice 9c, second half of the 9b
// right-click work: web/src/components/ElementContextMenu.jsx mounts one of
// these once its scoped "Ask Claude to…" prompt has posted).
//
// Layered on the EXISTING annotation state machine (web/src/useAnnotations.js
// — preview/accept/reject/retry/undo, the generation and session refs, the
// 5-event SSE allowlist) and the decision flow AnnotationDecisionCard.jsx
// already renders. This component does not reimplement that machine: it
// takes the SAME shaped props (annotation, busy, error, confirmation, the
// five callbacks) and adds the capsule anatomy on top — the proposal, the
// affected artifact, reversibility, live status, and the pre-accept
// containment check AnnotationDecisionCard never had.
//
// THE EPISTEMIC RULE: nothing here reads as settled until the receipt says
// so. The `applying` phase (busy===true, between a click and the server's
// reply) renders as explicitly provisional; only once `confirmation` lands
// (the accept/reject/retry/undo receipt) does the capsule reconcile to a
// settled phase, reading `annotation.state` as refreshed from the server.
//
// CONTAINMENT (Check 3): before Accept is offered, a re-parse diff scoped to
// the element's handle (web/src/lib/entityDelta.js — the same
// handle-or-content-hash identity server/routers/drawings.py's version-delta
// route uses). `resolveScopedIntakes(identity)` is an ADAPTER the mounting
// scene supplies, `(identity) => {baseIntake, candidateIntake} | null` — the
// SAME shape of injected-adapter contract useDrawingVersionController.js
// already uses for loadHead/loadVersion. Today's global mount
// (ElementContextMenu's ctx, wired from App.jsx's outer scope) does not wire
// one: an honest, named gap of exactly the kind that file's own header
// already documents for `ctx.session`/`ctx.reach` on Modify/Clipboard rows
// (those live inside the workspace card's own EngineSessionProvider subtree,
// narrower than this mount). Missing the adapter refuses accept with a real
// reason rather than accepting blind — never a fabricated pass.
import { useMemo, useState } from 'react'

import { computeEntityDelta, scopeDeltaToHandle } from '../lib/entityDelta.js'
import LiveRegion from './LiveRegion.jsx'

// The honesty-ladder-checked reason map for this file (discovered by name —
// see ElementContextMenu.jsx's CONTEXT_MENU_REASONS for the same contract).
export const CHANGE_CAPSULE_REASONS = Object.freeze({
  noContainmentForKind: 'No scoped containment check exists for this element kind yet, so accept stays refused.',
  noScopedDiff: 'The proposed content is not available to re-parse yet, so a scoped diff cannot be computed.',
  outOfScope: 'The proposed change reaches beyond this element, so accept is refused to stay contained.',
})

/**
 * `{annotation, busy, error}` -> one phase name. Pure and total: an
 * unrecognized `annotation.state` (a future server addition) reads as
 * 'stale' rather than throwing — the honest "something changed server-side
 * that this capsule does not have a rendering for yet" branch.
 */
export function capsulePhase({ annotation, busy, error }) {
  if (error) return 'error'
  if (busy) return 'applying'
  if (!annotation) return 'empty'
  if (annotation.state === 'pending') return 'pending'
  if (annotation.state === 'accepted') return 'accepted'
  if (annotation.state === 'rejected') return 'rejected'
  if (annotation.state === 'expired') return 'expired'
  return 'stale'
}

/**
 * The pre-accept containment verdict. `phase` must be 'pending' (Accept is
 * never offered outside that phase, so there is nothing to gate elsewhere);
 * `identity` is the element the capsule is attached to; `intakes` is either
 * `null` (no adapter, or the adapter answered "unavailable") or
 * `{baseIntake, candidateIntake}`.
 *
 * FAILS CLOSED at every branch: no identity, a non-'entity' kind (the only
 * kind this file's diff mechanism covers today), a missing adapter answer,
 * or an uncomputable delta all refuse with a real, distinct reason. Only a
 * delta whose every touched entity carries the target's exact handle allows.
 */
export function acceptGate({ identity, phase, intakes }) {
  if (phase !== 'pending') return { allowed: false, reason: null, outside: [] }
  if (!identity || identity.kind !== 'entity') {
    return { allowed: false, reason: CHANGE_CAPSULE_REASONS.noContainmentForKind, outside: [] }
  }
  if (!intakes || !intakes.baseIntake || !intakes.candidateIntake) {
    return { allowed: false, reason: CHANGE_CAPSULE_REASONS.noScopedDiff, outside: [] }
  }
  const delta = computeEntityDelta(intakes.baseIntake, intakes.candidateIntake)
  if (!delta) return { allowed: false, reason: CHANGE_CAPSULE_REASONS.noScopedDiff, outside: [] }
  const verdict = scopeDeltaToHandle(delta, identity.id)
  if (!verdict.scoped) return { allowed: false, reason: CHANGE_CAPSULE_REASONS.outOfScope, outside: verdict.outside }
  return { allowed: true, reason: null, outside: [] }
}

const PHASE_STATUS = Object.freeze({
  empty: 'No proposal yet.',
  pending: 'Awaiting your decision. Nothing has changed yet.',
  applying: 'Applying — not yet confirmed by the server.',
  accepted: 'Accepted and confirmed by the server.',
  rejected: 'Rejected. Nothing was applied.',
  expired: 'Expired before a decision was made. Nothing was applied.',
  error: 'The last action did not complete. Nothing changed.',
  stale: 'Status is out of date with the server.',
})

function reversibilityCopy(phase) {
  if (phase === 'accepted') return 'Reversible: Undo prepares an inverse of this exact change.'
  if (phase === 'pending' || phase === 'applying') return 'Reversible: rejecting leaves the drawing untouched.'
  if (phase === 'rejected' || phase === 'expired') return 'Reversible: retry proposes a fresh change from the current drawing.'
  return 'Reversibility unknown until a decision is confirmed.'
}

export default function ChangeCapsule({
  identity = null,
  annotation = null,
  busy = false,
  error = null,
  confirmation = null,
  onPreview,
  onAccept,
  onReject,
  onRetry,
  onUndo,
  onEdit,
  resolveScopedIntakes,
}) {
  const [dismissed, setDismissed] = useState(false)
  const phase = capsulePhase({ annotation, busy, error })

  const intakes = useMemo(() => {
    if (phase !== 'pending' || typeof resolveScopedIntakes !== 'function') return null
    return resolveScopedIntakes(identity) || null
    // Recomputed only when the identity or the batch changes — a fresh batch
    // can carry different proposed content even for the same element.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, identity?.kind, identity?.id, annotation?.batchId, resolveScopedIntakes])

  const gate = useMemo(() => acceptGate({ identity, phase, intakes }), [identity, phase, intakes])

  if (!annotation || dismissed) return null

  const confirmedCopy = confirmation && (
    confirmation.kind === 'undo'
      ? `Inverse confirmed at revision ${confirmation.revision}, target version ${confirmation.targetVersion}.`
      : `Confirmed at revision ${confirmation.revision}, target version ${confirmation.targetVersion}.`
  )

  return (
    <section className="overlay-card change-capsule" aria-label="Change capsule" data-testid="change-capsule">
      <div className="overlay-card-head">
        <span className="overlay-card-title">Change capsule</span>
        <button
          type="button"
          className="chip-act"
          data-testid="change-capsule-dismiss"
          onClick={() => setDismissed(true)}
        >
          Dismiss
        </button>
      </div>
      <p className="overlay-card-ask" data-testid="change-capsule-proposal">{annotation.decisionCopy}</p>
      <p className="dim" data-testid="change-capsule-artifact">
        {identity ? `Affects ${identity.kind} ${identity.id}` : 'No element attached.'}
      </p>
      {gate.outside.length > 0 && (
        <p className="overlay-card-error" role="alert" data-testid="change-capsule-outside">
          Also touches: {gate.outside.map((t) => t.handle || 'an unidentified entity').join(', ')}
        </p>
      )}
      <p className="dim" data-testid="change-capsule-reversibility">{reversibilityCopy(phase)}</p>
      <LiveRegion as="p" className="dim" data-testid="change-capsule-status">
        {PHASE_STATUS[phase] || PHASE_STATUS.stale}
      </LiveRegion>
      <div className="overlay-card-actions">
        <button type="button" className="chip-act" disabled={busy} onClick={onPreview}>
          {busy ? 'Checking…' : 'Preview current'}
        </button>
        {phase === 'pending' && (
          <>
            <button
              type="button"
              className="chip-act"
              disabled={busy || !gate.allowed}
              title={gate.allowed ? undefined : gate.reason}
              data-reason={gate.allowed ? undefined : gate.reason}
              data-testid="change-capsule-accept"
              onClick={onAccept}
            >
              Accept
            </button>
            <button type="button" className="chip-act" disabled={busy} onClick={onEdit} data-testid="change-capsule-edit">
              Edit
            </button>
            <button type="button" className="chip-act overlay-deny" disabled={busy} onClick={onReject}>
              Reject
            </button>
          </>
        )}
        {(phase === 'rejected' || phase === 'expired') && (
          <button type="button" className="chip-act" disabled={busy} onClick={onRetry}>
            Retry as a fresh proposal
          </button>
        )}
        {phase === 'accepted' && (
          <button type="button" className="chip-act" disabled={busy} onClick={onUndo} data-testid="change-capsule-undo">
            Prepare undo
          </button>
        )}
      </div>
      <LiveRegion as="p" className="dim">{confirmedCopy || ''}</LiveRegion>
      {error && <p className="overlay-card-error" role="alert">{error}</p>}
    </section>
  )
}
