/**
 * WorkerJobsPanel — "worker job status and cancellation" (Wave 2 Lane E
 * plan). This bounded panel only offers cancellation for an exact active
 * worker/run pair returned by the server. The server, not the browser,
 * resolves its owner, tenant, freshness, and terminal state.
 *
 * The commands field is free-text shell commands the operator is about to
 * run in the DISPOSABLE worker (contract/OPERATOR.md section 6); it is
 * never labeled or treated as a credential (acceptance #1).
 */
import { useState } from 'react'

import * as operatorClient from '../../operatorClient.js'
import { deepRedact, isRedactedField, renderFieldValue } from '../../projects/ReceiptPanel.jsx'

const CANCEL_DISABLED_REASON =
  'Cancellation is available only while this receipt identifies one active worker and run.'

// Mono, muted-key styling layered onto the existing `.operator-field-row`
// class (operator.css already gives dd font-mono + dt muted color; this
// just completes "key in muted mono" using only the dark-subtree's pinned
// --font-mono token, never a fresh color).
const KEY_STYLE = { fontFamily: 'var(--font-mono)' }

function cancelTarget(receipt) {
  const workerId = receipt?.worker_id ?? receipt?.workerId
  const runId = receipt?.run_id ?? receipt?.runId
  const active = receipt?.status === 'running' || receipt?.status === 'accepted'
  return active && typeof workerId === 'string' && typeof runId === 'string'
    ? { workerId, runId }
    : null
}

function isScalar(v) {
  return v == null || typeof v !== 'object'
}

// True once a field's value nests a non-scalar a second level down (an
// object-of-objects, or an array holding non-scalar items) — deeper than
// the flat/one-level-indented rows below render honestly, so the panel also
// offers the redacted raw fallback for that case.
function exceedsOneLevel(value) {
  if (Array.isArray(value)) return value.some((v) => !isScalar(v))
  if (value && typeof value === 'object') return Object.values(value).some((v) => !isScalar(v))
  return false
}

function scalarDisplay(fieldKey, value) {
  return isRedactedField(fieldKey, value) ? '[redacted]' : renderFieldValue(value)
}

function arrayDisplay(fieldKey, value) {
  if (value.length === 0) return '(empty)'
  return value.map((v) => scalarDisplay(fieldKey, v)).join(', ')
}

// One labeled row for a top-level receipt field: scalars render inline,
// arrays of scalars render joined, and a plain object one level deep
// renders as indented sub-rows right below it (each sub-value redacted the
// same way as a top-level one).
function ReceiptFieldRows({ fieldKey, value }) {
  if (isRedactedField(fieldKey, value)) {
    return (
      <div className="operator-field-row">
        <dt style={KEY_STYLE}>{fieldKey}</dt>
        <dd>[redacted]</dd>
      </div>
    )
  }
  if (Array.isArray(value)) {
    return (
      <div className="operator-field-row">
        <dt style={KEY_STYLE}>{fieldKey}</dt>
        <dd>{arrayDisplay(fieldKey, value)}</dd>
      </div>
    )
  }
  if (value && typeof value === 'object') {
    const sub = Object.entries(value)
    return (
      <>
        <div className="operator-field-row">
          <dt style={KEY_STYLE}>{fieldKey}</dt>
          <dd>{sub.length === 0 ? '(empty)' : null}</dd>
        </div>
        {sub.map(([subKey, subValue]) => (
          <div key={`${fieldKey}.${subKey}`} className="operator-field-row" style={{ marginLeft: 14 }}>
            <dt style={KEY_STYLE}>{subKey}</dt>
            <dd>{isScalar(subValue) ? scalarDisplay(subKey, subValue) : '…'}</dd>
          </div>
        ))}
      </>
    )
  }
  return (
    <div className="operator-field-row">
      <dt style={KEY_STYLE}>{fieldKey}</dt>
      <dd>{scalarDisplay(fieldKey, value)}</dd>
    </div>
  )
}

// The receipt as labeled rows (reusing ReceiptPanel's exact redaction
// denylist — see the module import above). A receipt that nests a value
// beyond one level also gets a `<details>` raw-JSON fallback, itself run
// through `deepRedact` so a token buried two levels down can never leak via
// the "show me everything" escape hatch.
function ReceiptFields({ receipt }) {
  const entries = Object.entries(receipt || {})
  const needsRaw = entries.some(([, v]) => exceedsOneLevel(v))
  return (
    <>
      <dl className="operator-readout">
        {entries.map(([fieldKey, value]) => (
          <ReceiptFieldRows key={fieldKey} fieldKey={fieldKey} value={value} />
        ))}
      </dl>
      {needsRaw && (
        <details>
          <summary className="dim" style={{ cursor: 'pointer' }}>Raw receipt</summary>
          <pre style={{ fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
            {JSON.stringify(deepRedact(receipt), null, 2)}
          </pre>
        </details>
      )}
    </>
  )
}

export default function WorkerJobsPanel({ onSignedOut }) {
  const [commandsText, setCommandsText] = useState('')
  const [receipt, setReceipt] = useState(null)
  const [dispatching, setDispatching] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [error, setError] = useState(null)

  const dispatch = async () => {
    const commands = commandsText.split('\n').map((l) => l.trim()).filter(Boolean)
    if (commands.length === 0 || dispatching) return
    setDispatching(true)
    setError(null)
    try {
      setReceipt(await operatorClient.dispatchWorker(commands))
    } catch (e) {
      setError(e?.body?.detail || e?.message || 'The job did not dispatch — nothing ran.')
      if (operatorClient.isOperatorDenied(e)) onSignedOut?.()
    } finally {
      setDispatching(false)
    }
  }

  const cancel = async () => {
    const target = cancelTarget(receipt)
    if (!target || cancelling) return
    setCancelling(true)
    setError(null)
    try {
      setReceipt(await operatorClient.cancelWorker(target.workerId, target.runId))
    } catch (e) {
      setError(e?.body?.detail || e?.message || 'The worker did not cancel.')
      if (operatorClient.isOperatorDenied(e)) onSignedOut?.()
    } finally {
      setCancelling(false)
    }
  }

  const target = cancelTarget(receipt)

  return (
    <section className="operator-panel operator-worker-panel" aria-label="Worker jobs">
      <h2>Worker jobs</h2>

      <label htmlFor="operator-worker-commands">Commands (one per line)</label>
      <textarea
        id="operator-worker-commands"
        value={commandsText}
        onChange={(e) => setCommandsText(e.target.value)}
        disabled={dispatching}
      />
      <button type="button" className="chip-act" disabled={dispatching || !commandsText.trim()} onClick={dispatch}>
        {dispatching ? 'Dispatching…' : 'Dispatch job'}
      </button>

      {receipt && (
        <div className="operator-job-receipt">
          <h3>Last dispatch receipt</h3>
          <ReceiptFields receipt={receipt} />
          <button
            type="button"
            className="chip-act operator-cancel"
            disabled={!target || cancelling}
            aria-label={target ? 'Cancel active worker' : `Cancel (unavailable — ${CANCEL_DISABLED_REASON})`}
            title={target ? 'Cancel this exact active worker run' : CANCEL_DISABLED_REASON}
            onClick={cancel}
          >
            {cancelling ? 'Cancelling…' : 'Cancel'}
          </button>
          {!target && <p className="dim">{CANCEL_DISABLED_REASON}</p>}
        </div>
      )}

      {error && <p className="operator-panel-error" role="alert">{error}</p>}
    </section>
  )
}
