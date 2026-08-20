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

const CANCEL_DISABLED_REASON =
  'Cancellation is available only while this receipt identifies one active worker and run.'

function cancelTarget(receipt) {
  const workerId = receipt?.worker_id ?? receipt?.workerId
  const runId = receipt?.run_id ?? receipt?.runId
  const active = receipt?.status === 'running' || receipt?.status === 'accepted'
  return active && typeof workerId === 'string' && typeof runId === 'string'
    ? { workerId, runId }
    : null
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
          <pre>{JSON.stringify(receipt, null, 2)}</pre>
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
