/**
 * ApprovalCard — the operator's ONE surface for deciding a high-impact
 * (always-confirm) action, built ONLY from a runbook `propose` response
 * normalized by approvalDigest.js. contract/OPERATOR.md section 5.
 *
 * Acceptance #3: renders target, environment, cost, scope, expiry, reversal,
 * and argument digest FROM THE PROPOSE PAYLOAD, never a client default; any
 * missing field refuses to render an Execute control and names what's
 * missing instead.
 *
 * Acceptance #2: every actionable control is keyboard-reachable with an
 * accessible name; opening the confirm dialog moves focus INTO it, and
 * closing it WITHOUT acting (Cancel) returns focus to the Execute button
 * that opened it — the standard dialog pattern, run from a useEffect so the
 * refocus always targets the post-commit (correctly enabled/disabled) DOM
 * node rather than racing React's render.
 *
 * This card has no text inputs at all — nothing here could ever carry a
 * secret/token/password/key value even by accident (acceptance #1).
 */
import { useEffect, useRef, useState } from 'react'

export default function ApprovalCard({
  action,          // e.g. "operator.tenant_agent_pause"
  authorityId,
  digest,          // { target, environment, cost, scope, expiry, reversal, argsDigest }
  missing = [],    // field names (approvalDigest.REQUIRED_FIELDS subset) absent from digest
  fieldLabel = (f) => f,
  onExecute,       // async (authorityId) => void
  busy = false,
}) {
  const [confirming, setConfirming] = useState(false)
  const [executing, setExecuting] = useState(false)
  const [error, setError] = useState(null)
  const executeButtonRef = useRef(null)
  const confirmButtonRef = useRef(null)
  const wasConfirmingRef = useRef(false)

  // Runs AFTER the DOM commits, so the target node's disabled/enabled state
  // is already correct — an imperative .focus() call inside the click
  // handler itself would race React's re-render and silently no-op against
  // a still-disabled button.
  useEffect(() => {
    if (confirming && !wasConfirmingRef.current) {
      confirmButtonRef.current?.focus()
    } else if (!confirming && wasConfirmingRef.current) {
      executeButtonRef.current?.focus()
    }
    wasConfirmingRef.current = confirming
  }, [confirming])

  if (!action || !authorityId) return null

  const canExecute = missing.length === 0

  const openConfirm = () => {
    if (!canExecute || executing || busy) return
    setError(null)
    setConfirming(true)
  }

  const cancelConfirm = () => {
    if (executing) return
    setConfirming(false)
  }

  const confirmExecute = async () => {
    if (executing) return
    setExecuting(true)
    setError(null)
    try {
      await onExecute(authorityId)
      setConfirming(false)
    } catch (e) {
      // Leave the confirm dialog OPEN on failure — closing it would tell the
      // operator the action landed when it did not (OverlayDecisionCard's
      // same rule, applied to a higher-blast-radius surface). `confirming`
      // never changes on this path, so the useEffect above leaves focus
      // exactly where it was: inside the dialog, ready to retry.
      setError(e?.body?.detail || e?.message || 'The action did not go through — nothing changed.')
    } finally {
      setExecuting(false)
    }
  }

  return (
    <div className="operator-approval-card" role="group" aria-label={`Approval for ${action}`}>
      <div className="operator-approval-head">
        <span className="operator-approval-action">{action}</span>
        {/* Blast radius, at a glance, before reading a single field: which
            environment this authority reaches. */}
        {digest.environment !== undefined && <span className="tag">{digest.environment}</span>}
      </div>

      <dl className="operator-approval-fields">
        {['target', 'environment', 'cost', 'scope', 'expiry', 'reversal', 'argsDigest'].map((field) => (
          <div
            className={
              field === 'cost' || field === 'scope'
                ? 'operator-approval-field operator-approval-field-strong'
                : 'operator-approval-field'
            }
            key={field}
          >
            <dt>{fieldLabel(field)}</dt>
            <dd>
              {digest[field] !== undefined
                ? <code>{String(digest[field])}</code>
                : <span className="operator-approval-missing">missing</span>}
            </dd>
          </div>
        ))}
      </dl>

      {!canExecute && (
        <p className="operator-approval-blocked" role="status">
          Cannot render an Execute control — missing {missing.map(fieldLabel).join(', ')}.
        </p>
      )}

      {canExecute && (
        // Stays MOUNTED (just disabled) while the confirm dialog is open, on
        // purpose: unmounting it would null out executeButtonRef, and the
        // focus-return effect above would have nothing to focus.
        <button
          type="button"
          ref={executeButtonRef}
          className="chip-act"
          disabled={busy || confirming}
          onClick={openConfirm}
        >
          Review &amp; Execute
        </button>
      )}

      {canExecute && confirming && (
        <div
          className="operator-approval-confirm"
          role="alertdialog"
          aria-label={`Confirm ${action} on ${digest.target}`}
        >
          <p>
            Execute <strong>{action}</strong> on <strong>{digest.target}</strong> in{' '}
            <strong>{digest.environment}</strong>? Reversal: {digest.reversal}.
          </p>
          <div className="operator-approval-confirm-actions">
            <button type="button" ref={confirmButtonRef} className="chip-act" onClick={confirmExecute}>
              {executing ? 'Executing…' : 'Confirm Execute'}
            </button>
            <button type="button" className="chip-act operator-cancel" disabled={executing} onClick={cancelConfirm}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <p className="operator-approval-error" role="alert">{error}</p>}
    </div>
  )
}
