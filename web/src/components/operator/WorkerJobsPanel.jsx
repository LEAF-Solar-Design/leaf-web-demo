/**
 * WorkerJobsPanel — "worker job status and cancellation" (Wave 2 Lane E
 * plan). server/routers/operator_worker.py exposes ONLY
 * POST /api/operator/worker/dispatch on main: no GET status/list route and
 * no cancel route exist. So this panel can only ever show the receipt from
 * the dispatch call the console itself made, and Cancel is a disabled
 * control with the reason spelled out — never a fabricated "cancelled".
 *
 * The commands field is free-text shell commands the operator is about to
 * run in the DISPOSABLE worker (contract/OPERATOR.md section 6); it is
 * never labeled or treated as a credential (acceptance #1).
 */
import { useState } from 'react'

import * as operatorClient from '../../operatorClient.js'

const CANCEL_DISABLED_REASON =
  'No worker-cancel endpoint is mounted on main (operator.worker_cancel_job is declared "v1: on" in the ' +
  'contract but server/routers/operator_worker.py exposes only POST /dispatch) — flagged as a follow-up.'

export default function WorkerJobsPanel({ onSignedOut }) {
  const [commandsText, setCommandsText] = useState('')
  const [receipt, setReceipt] = useState(null)
  const [dispatching, setDispatching] = useState(false)
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
          <pre>{JSON.stringify(receipt, null, 2)}</pre>
          <button
            type="button"
            className="chip-act operator-cancel"
            disabled
            aria-label={`Cancel (unavailable — ${CANCEL_DISABLED_REASON})`}
            title={CANCEL_DISABLED_REASON}
          >
            Cancel
          </button>
          <p className="dim">{CANCEL_DISABLED_REASON}</p>
        </div>
      )}

      {error && <p className="operator-panel-error" role="alert">{error}</p>}
    </section>
  )
}
