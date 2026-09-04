import LiveRegion from './LiveRegion.jsx'

export default function AnnotationDecisionCard({
  annotation,
  busy = false,
  error = null,
  confirmation = null,
  onPreview,
  onAccept,
  onReject,
  onRetry,
  onUndo,
}) {
  if (!annotation) return null

  const pending = annotation.state === 'pending'
  const retryable = annotation.state === 'rejected' || annotation.state === 'expired'
  const undoable = annotation.state === 'accepted'
  const confirmed = confirmation && (
    confirmation.kind === 'undo'
      ? `Inverse confirmed at revision ${confirmation.revision}, target version ${confirmation.targetVersion}, head ${confirmation.targetCommit}.`
      : confirmation.retryOfBatchId
        ? `Fresh proposal ${confirmation.batchId}, revision ${confirmation.revision}, head ${confirmation.previewCommit}.`
        : `Server confirmed revision ${confirmation.revision}, head ${confirmation.targetCommit}.`
  )

  return (
    <section className="overlay-card" aria-label="Annotation decision">
      <div className="overlay-card-head">
        <span className="overlay-card-title">Annotation decision</span>
      </div>
      <p className="overlay-card-ask">{annotation.decisionCopy}</p>
      <p className="dim">
        Batch {annotation.batchId}, revision {annotation.revision}, target version {annotation.targetVersion}
      </p>
      <div className="overlay-card-actions">
        <button type="button" className="chip-act" disabled={busy} onClick={onPreview}>
          {busy ? 'Checking…' : 'Preview current'}
        </button>
        {pending && (
          <>
            <button type="button" className="chip-act" disabled={busy} onClick={onAccept}>
              Accept
            </button>
            <button type="button" className="chip-act overlay-deny" disabled={busy} onClick={onReject}>
              Reject
            </button>
          </>
        )}
        {retryable && (
          <button type="button" className="chip-act" disabled={busy} onClick={onRetry}>
            Retry as a fresh proposal
          </button>
        )}
        {undoable && (
          <button type="button" className="chip-act" disabled={busy} onClick={onUndo}>
            Prepare undo
          </button>
        )}
      </div>
      <LiveRegion as="p" className="dim">{confirmed || ''}</LiveRegion>
      {error && <p className="overlay-card-error" role="alert">{error}</p>}
    </section>
  )
}
