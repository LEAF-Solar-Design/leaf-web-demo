/**
 * CloneDialog — clone a project into a new one owned by the cloner.
 *
 * Card B-U3 acceptance:
 *   Clone produces a new project owned by the cloner; progress and failure
 *   states surfaced; receipt id shown on completion.
 *
 * Purely props-driven (Membership.jsx pattern): this component owns no
 * fetch. `onClone` is the parent's real server call; the receipt it resolves
 * with is the server's answer to "what did you create and who owns it" —
 * ownership and the receipt id are read off that response, never guessed
 * client-side. Cloning/failed/done are internal UI state only, not server
 * truth, because they describe this one in-flight request.
 */
import { useState } from 'react'

function errorMessage(e, fallback) {
  return e?.body?.detail || e?.message || fallback
}

export default function CloneDialog({
  open,
  project,       // { project_id, name } — the project being cloned
  onClone,       // async (projectId) => receipt: { receipt_id, project_id, name, owner_id, owner_name }
  onClose,
}) {
  const [status, setStatus] = useState('idle') // idle | cloning | done | failed
  const [error, setError] = useState(null)
  const [receipt, setReceipt] = useState(null)

  if (!open || !project) return null

  const startClone = async () => {
    if (status === 'cloning') return
    setStatus('cloning')
    setError(null)
    setReceipt(null)
    try {
      const result = await onClone(project.project_id)
      setReceipt(result)
      setStatus('done')
    } catch (e) {
      setError(errorMessage(e, 'The clone did not go through — nothing changed.'))
      setStatus('failed')
    }
  }

  const closeAndReset = () => {
    if (status === 'cloning') return
    setStatus('idle')
    setError(null)
    setReceipt(null)
    onClose?.()
  }

  return (
    <div className="clone-dialog-backdrop" role="presentation">
      <section className="clone-dialog" role="dialog" aria-modal="true" aria-label={`Clone ${project.name}`}>
        <div className="clone-dialog-head">
          <span className="clone-dialog-title">Clone "{project.name}"</span>
          <button
            type="button"
            className="chip-act clone-dialog-close"
            onClick={closeAndReset}
            disabled={status === 'cloning'}
          >
            Close
          </button>
        </div>

        {status === 'idle' && (
          <p className="clone-dialog-body">
            This creates a new project you own, copied from "{project.name}".
          </p>
        )}

        {status === 'cloning' && (
          <p className="clone-dialog-progress" role="status">
            Cloning "{project.name}"…
          </p>
        )}

        {status === 'failed' && (
          <p className="clone-dialog-error" role="alert">{error}</p>
        )}

        {status === 'done' && receipt && (
          <div className="clone-dialog-done" role="status">
            <p className="clone-dialog-success">
              Clone complete: "{receipt.name || project.name}", owned by{' '}
              {receipt.owner_name || receipt.owner_id || 'you'}.
            </p>
            <p className="clone-dialog-receipt">
              Receipt: <span className="clone-dialog-receipt-id">{receipt.receipt_id}</span>
            </p>
          </div>
        )}

        {(status === 'idle' || status === 'failed') && (
          <button type="button" className="chip-act clone-dialog-start" onClick={startClone}>
            {status === 'failed' ? 'Retry clone' : 'Clone project'}
          </button>
        )}
      </section>
    </div>
  )
}
