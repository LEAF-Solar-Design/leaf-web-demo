// Surface-config proposals check a fresh live manifest and its base digest
// before accept, commit through the tenant fold, and display its file receipt.
// Entity proposals retain the injected scoped-delta containment check below.
// Surface-config records carry kind: 'surface-config', overlay and baseSha256
// (null for an absent base file). Legacy records default to kind: 'entity'.
// Producing surface-config proposals in the entity Ask row remains separate.
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
import { useMemo, useRef, useState } from 'react'

import { getSurfaceConfig, submitSurfaceConfig } from '../api.js'
import { isSurfaceConfigOverlay, refreshSurfaceConfigOverlay } from '../site/useSurfaceContract.js'
import { computeEntityDelta, scopeDeltaToHandle } from '../lib/entityDelta.js'
import LiveRegion from './LiveRegion.jsx'

// The honesty-ladder-checked reason map for this file (discovered by name —
// see ElementContextMenu.jsx's CONTEXT_MENU_REASONS for the same contract).
export const CHANGE_CAPSULE_REASONS = Object.freeze({
  noContainmentForKind: 'No scoped containment check exists for this element kind yet, so accept stays refused.',
  noScopedDiff: 'The proposed content is not available to re-parse yet, so a scoped diff cannot be computed.',
  outOfScope: 'The proposed change reaches beyond this element, so accept is refused to stay contained.',
  staleBase: 'The surface config has changed since this proposal was made. Accept is refused; request a fresh proposal.',
  invalidManifest: 'The live surface config or proposed overlay has an unknown slot or invalid shape. Accept is refused.',
  manifestUnavailable: 'The live surface config could not be checked. Nothing was submitted.',
  commitFailed: 'The commit did not confirm; refresh to see whether it landed',
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

export default function ChangeCapsule(props) {
  return <ChangeCapsuleRecord key={`${props.annotation?.batchId}:${props.annotation?.revision}`} {...props} />
}

function ChangeCapsuleRecord({
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
  const [submitting, setSubmitting] = useState(false)
  const [refusal, setRefusal] = useState(null)
  const [receipt, setReceipt] = useState(null)
  const [commitOutcome, setCommitOutcome] = useState(null)
  const [submittedSha256, setSubmittedSha256] = useState(null)
  const [liveSha256, setLiveSha256] = useState(null)
  const inFlight = useRef(false)
  const kind = annotation?.kind || 'entity'
  const isSurfaceConfig = kind === 'surface-config'
  const phase = commitOutcome === 'committed' ? 'accepted'
    : commitOutcome === 'unconfirmed' ? 'unconfirmed'
      : capsulePhase({ annotation, busy: busy || submitting, error })

  const intakes = useMemo(() => {
    if (phase !== 'pending' || typeof resolveScopedIntakes !== 'function') return null
    return resolveScopedIntakes(identity) || null
    // Recomputed only when the identity or the batch changes — a fresh batch
    // can carry different proposed content even for the same element.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, identity?.kind, identity?.id, annotation?.batchId, resolveScopedIntakes])

  const gate = useMemo(() => isSurfaceConfig
    ? { allowed: phase === 'pending' && !refusal, reason: refusal, outside: [] }
    : acceptGate({ identity, phase, intakes }), [identity, phase, intakes, isSurfaceConfig, refusal])

  async function acceptSurfaceConfig() {
    if (inFlight.current || receipt || !gate.allowed) return
    inFlight.current = true
    setSubmitting(true)
    let submitted = false
    try {
      const live = await getSurfaceConfig(false, { fresh: true })
      if (!isSurfaceConfigOverlay(live?.surfaces) || !isSurfaceConfigOverlay(annotation.overlay)) {
        setRefusal(CHANGE_CAPSULE_REASONS.invalidManifest)
        return
      }
      if (annotation.baseSha256 !== (live?.source?.sha256 ?? null)) {
        setRefusal(CHANGE_CAPSULE_REASONS.staleBase)
        return
      }
      const document = JSON.stringify(annotation.overlay, null, 2) + '\n'
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(document))
      setSubmittedSha256(Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join(''))
      submitted = true
      setCommitOutcome('unconfirmed')
      const committed = await submitSurfaceConfig(JSON.parse(document))
      if (typeof committed?.sha256 !== 'string' || typeof committed?.authored_at !== 'string') {
        throw new Error('Missing file receipt')
      }
      setReceipt(committed)
      setCommitOutcome('committed')
      await refreshSurfaceConfigOverlay()
    } catch {
      setRefusal(submitted ? CHANGE_CAPSULE_REASONS.commitFailed : CHANGE_CAPSULE_REASONS.manifestUnavailable)
    } finally {
      inFlight.current = false
      setSubmitting(false)
    }
  }

  async function refreshCommit() {
    if (inFlight.current) return
    inFlight.current = true
    setSubmitting(true)
    try {
      await refreshSurfaceConfigOverlay()
      const live = await getSurfaceConfig(false, { fresh: true })
      setLiveSha256(live?.source?.sha256 ?? null)
      if (submittedSha256 && live?.source?.sha256 === submittedSha256
          && typeof live.source.authored_at === 'string') {
        setReceipt(live.source)
        setCommitOutcome('committed')
        setRefusal(null)
      }
    } catch {
      setRefusal(CHANGE_CAPSULE_REASONS.commitFailed)
    } finally {
      inFlight.current = false
      setSubmitting(false)
    }
  }

  if (!annotation || dismissed) return null

  const confirmedCopy = receipt
    ? `Surface config committed: ${receipt.sha256.slice(0, 8)}, authored ${receipt.authored_at}.`
    : confirmation && (
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
        {isSurfaceConfig ? 'Affects the workspace surface config.' : identity ? `Affects ${identity.kind} ${identity.id}` : 'No element attached.'}
      </p>
      {gate.outside.length > 0 && (
        <p className="overlay-card-error" role="alert" data-testid="change-capsule-outside">
          Also touches: {gate.outside.map((t) => t.handle || 'an unidentified entity').join(', ')}
        </p>
      )}
      <p className="dim" data-testid="change-capsule-reversibility">{isSurfaceConfig
        ? 'Accept replaces the surface config file. Restoring it requires a new proposal.'
        : reversibilityCopy(phase)}</p>
      <LiveRegion as="p" className="dim" data-testid="change-capsule-status">
        {phase === 'unconfirmed' ? CHANGE_CAPSULE_REASONS.commitFailed : PHASE_STATUS[phase] || PHASE_STATUS.stale}
      </LiveRegion>
      <div className="overlay-card-actions">
        {commitOutcome === 'unconfirmed' && (
          <button type="button" className="chip-act" disabled={submitting} onClick={refreshCommit}>
            Refresh
          </button>
        )}
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
              onClick={isSurfaceConfig ? acceptSurfaceConfig : onAccept}
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
        {phase === 'accepted' && !isSurfaceConfig && (
          <button type="button" className="chip-act" disabled={busy} onClick={onUndo} data-testid="change-capsule-undo">
            Prepare undo
          </button>
        )}
      </div>
      <LiveRegion as="p" className="dim">{confirmedCopy || ''}</LiveRegion>
      {commitOutcome === 'unconfirmed' && liveSha256 && <p className="dim">Live SHA-256: {liveSha256}</p>}
      {error && <p className="overlay-card-error" role="alert">{error}</p>}
      {refusal && <p className="overlay-card-error" role="alert">{refusal}</p>}
    </section>
  )
}
