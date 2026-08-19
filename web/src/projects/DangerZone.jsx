/**
 * DangerZone — reset (delete all files) and delete (remove the project),
 * each behind a typed confirmation naming the project, each showing the
 * server's receipt on success.
 *
 * Card B-U5 acceptance:
 *   Delete requires typed confirmation naming the project; reset distinct
 *   from delete; both show the server receipt.
 *   Undo affordance surfaces exactly what the server contract offers (no
 *   fake undo: if the API is terminal, the UI says so before confirm).
 *
 * Purely props-driven (ExportDialog.jsx / Membership.jsx pattern): this
 * component owns no fetch. `onReset`/`onDelete` are the only things that
 * talk to a server, and `resetUndo`/`deleteUndo` are the server's own
 * account of what — if anything — can be undone for that action; the
 * component never invents an undo path the caller did not hand it. The
 * platform's actual reset/delete routes (platform/project_lifecycle.py
 * reset_project/delete_project) mint no restore token, so a caller wiring
 * this component against that API passes no undo prop and both actions
 * render as terminal — "This can't be undone" — before the typed
 * confirmation is even shown.
 */
import { useState } from 'react'

function errorMessage(e, fallback) {
  return e?.body?.detail || e?.message || fallback
}

function DangerAction({
  kind, // 'reset' | 'delete' — used only for aria labels/test hooks, never shared state
  projectName,
  heading,
  description,
  confirmLabel,
  undo, // null/undefined (terminal, no undo) | { description: string }
  onRun, // async () => result with result.receipt.receipt_id
}) {
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [receipt, setReceipt] = useState(null)

  const matches = typed.trim() === projectName

  const start = () => {
    setOpen(true)
    setTyped('')
    setError(null)
  }

  const cancel = () => {
    setOpen(false)
    setTyped('')
    setError(null)
  }

  const run = async () => {
    if (!matches || busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await onRun()
      setReceipt(result?.receipt || null)
      setOpen(false)
      setTyped('')
    } catch (e) {
      setError(errorMessage(e, `${kind === 'delete' ? 'Delete' : 'Reset'} did not go through — nothing changed.`))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`danger-action danger-action-${kind}`} aria-label={heading}>
      <div className="danger-action-head">
        <span className="danger-action-title">{heading}</span>
        <p className="danger-action-copy">{description}</p>
      </div>

      {!open && (
        <button type="button" className="chip-danger" onClick={start}>
          {confirmLabel}
        </button>
      )}

      {open && (
        <div className="danger-action-confirm">
          {undo ? (
            <p className="danger-action-undo">{undo.description}</p>
          ) : (
            <p className="danger-action-undo danger-action-terminal" role="alert">
              This can&apos;t be undone.
            </p>
          )}

          <label className="danger-action-typed">
            Type <strong>{projectName}</strong> to confirm
            <input
              type="text"
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              disabled={busy}
              aria-label={`Type the project name to confirm ${kind}`}
            />
          </label>

          <div className="danger-action-buttons">
            <button
              type="button"
              className="chip-danger-confirm"
              disabled={!matches || busy}
              onClick={run}
            >
              {busy ? `${confirmLabel}…` : confirmLabel}
            </button>
            <button type="button" className="chip-neutral" disabled={busy} onClick={cancel}>
              Cancel
            </button>
          </div>

          {error && <p className="danger-action-error" role="alert">{error}</p>}
        </div>
      )}

      {receipt && (
        <p className="danger-action-receipt" role="status">
          Receipt: <span className="danger-action-receipt-id">{receipt.receipt_id}</span>
        </p>
      )}
    </div>
  )
}

export default function DangerZone({
  projectName,
  resetUndo = null,
  deleteUndo = null,
  onReset,
  onDelete,
}) {
  if (!projectName) return null // no confirmed project identity yet — render nothing, never a guessed name

  return (
    <section className="danger-zone" aria-label="Danger zone">
      <DangerAction
        kind="reset"
        projectName={projectName}
        heading="Reset project"
        description="Deletes every file in this project. The project itself, its members, and its history stay."
        confirmLabel="Reset"
        undo={resetUndo}
        onRun={onReset}
      />
      <DangerAction
        kind="delete"
        projectName={projectName}
        heading="Delete project"
        description="Removes this project and revokes every member's access."
        confirmLabel="Delete"
        undo={deleteUndo}
        onRun={onDelete}
      />
    </section>
  )
}
