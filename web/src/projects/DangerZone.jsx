/** Archive and restore use server state; permanent deletion requires the exact name.
 * Reset keeps its existing independent typed confirmation and receipt flow.
 * This component owns no fetch and never retries a failed action.
 */
import { useLayoutEffect, useRef, useState } from 'react'
import { humanizeError } from '../errorHumanize.js'

function errorMessage(e, fallback) {
  return humanizeError(typeof e?.body?.detail === 'string' ? e.body.detail : e || fallback)
}

function DangerAction({
  kind, // used only for aria labels/test hooks, never shared state
  projectName,
  heading,
  description,
  confirmLabel,
  requiresName = true,
  undo, // null/undefined (terminal, no undo) | { description: string }
  onRun, // async () => result with result.receipt.receipt_id
}) {
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [receipt, setReceipt] = useState(null)
  const triggerRef = useRef(null)
  const inputRef = useRef(null)
  const confirmRef = useRef(null)
  const openedRef = useRef(false)

  useLayoutEffect(() => {
    if (open) {
      openedRef.current = true
      const target = inputRef.current || confirmRef.current
      target?.focus()
    } else if (openedRef.current) {
      triggerRef.current?.focus()
    }
  }, [open])

  const matches = !requiresName || (kind === 'delete' ? typed === projectName : typed.trim() === projectName)

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
      const result = await onRun(...(kind === 'delete' ? [typed] : []))
      setReceipt(result?.receipt || null)
      setOpen(false)
      setTyped('')
    } catch (e) {
      setError(errorMessage(e, `${heading} did not go through. Nothing changed.`))
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
        <button ref={triggerRef} type="button" className="chip-danger" onClick={start}>
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

          {requiresName && <label className="danger-action-typed">
            Type <strong>{projectName}</strong> to confirm
            <input
              ref={inputRef}
              type="text"
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              disabled={busy}
              aria-label={`Type the project name to confirm ${kind}`}
            />
          </label>}

          <div className="danger-action-buttons">
            <button
              ref={confirmRef}
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

function RestoreAction({ onRestore }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [receipt, setReceipt] = useState(null)
  const restore = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const result = await onRestore()
      setReceipt(result?.receipt || null)
    } catch (e) {
      setError(errorMessage(e, 'Restore did not go through. Nothing changed.'))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div aria-label="Restore project">
      <button type="button" disabled={busy} onClick={restore}>{busy ? 'Restoring…' : 'Restore'}</button>
      {error && <p role="alert">{error}</p>}
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
  projectStatus = 'active',
  archivedAt,
  restoreUntil,
  onArchive,
  onRestore,
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
      {projectStatus === 'archived' ? (
        <>
          <p>Archived on {archivedAt} · restorable until {restoreUntil}</p>
          <RestoreAction onRestore={onRestore} />
          <DangerAction
            kind="delete"
            projectName={projectName}
            heading="Delete permanently"
            description="Deletes this project permanently and revokes every member's access. Deletion is permanent and cannot be undone."
            confirmLabel="Delete permanently"
            onRun={onDelete}
          />
        </>
      ) : (
        <DangerAction
          kind="archive"
          projectName={projectName}
          heading="Archive project"
          description="Hidden from lists and read-only once archived. Restorable for 30 days."
          confirmLabel="Archive project"
          requiresName={false}
          undo={{ description: 'You can restore this project for 30 days.' }}
          onRun={onArchive}
        />
      )}
    </section>
  )
}
