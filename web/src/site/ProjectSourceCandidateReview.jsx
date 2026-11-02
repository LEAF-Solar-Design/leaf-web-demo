import { useLifecycleDialog } from '../projects/CloneDialog.jsx'
import { sourceMaterialProblem } from './useProjectSourceController.js'
import { validateProjectSource } from '../api.js'

export default function ProjectSourceCandidateReview({ controller }) {
  const c = controller
  const native = c.sourceContext === 'native'
  const title = native ? 'iOS project source' : 'Browser project source'
  const dialog = useLifecycleDialog(c.isOpen, c.close, native ? 'native-project-source' : 'browser-project-source')
  if (!c.isOpen) return null
  const row = c.candidate
  const witness = row && { source_request_id: row.source_request_id, receipt_digest: row.receipt_digest, version: row.version }
  const reviewable = !c.unknown && ['staged', 'awaiting_confirmation'].includes(row?.state)
  const disabled = c.busy || c.exportBusy || c.loading || c.materialsLoading
  let exportable = false
  try { exportable = !native && row?.state === 'published' && !!validateProjectSource(row, c.projectId) } catch { /* Invalid readback cannot be exported. */ }
  const downloadReady = exportable && !c.unknown && !c.loading && c.sourceLoaded && c.download
    && c.download.sourceRequestId === row.source_request_id && c.download.version === row.version
    && c.download.receiptDigest === row.receipt_digest && c.download.sourceCommit === row.source_commit
    && c.download.sourceTree === row.source_tree && c.download.projectId === c.projectId
  const frozen = c.unknown || c.candidates.some((item) => ['staged', 'awaiting_confirmation', 'publishing'].includes(item.state))
  const displayedIntent = c.intent && (!row || c.intent.source_request_id === row.source_request_id) ? c.intent : null
  return <div className="clone-dialog-backdrop" role="presentation">
    <section ref={dialog} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}
      className="clone-dialog" style={{ boxSizing: 'border-box', width: 'min(640px, calc(100vw - 24px))', maxWidth: '100%', maxHeight: 'calc(100dvh - 24px)', display: 'flex', flexDirection: 'column', overflow: 'hidden', overflowWrap: 'anywhere' }}>
      <div className="clone-dialog-head" style={{ flexShrink: 0 }}><span className="clone-dialog-title">{title}</span>
        <button type="button" className="chip-act" onClick={c.close}>Close</button></div>
      <div style={{ overflowY: 'auto', minHeight: 0, padding: 12 }}>
        {native && <><p>iOS source supports creation only. Choose one through eight nonempty TXT or JSON project originals, at most 1 MiB each and 4 MiB total.</p>
          <p>Only selected text or JSON references are used. Retained movies are unavailable to this source adapter; a transcript is a text reference, not video processing.</p>
          <p>iOS app build, package, simulator/device runtime and delivery remain pending. Source download is unavailable.</p>
          {c.createOnlyBlocked && <p role="status">iOS source currently supports creation only. Editing saved iOS source is unavailable.</p>}</>}
        <label>Original requirements
          <textarea tabIndex={0} aria-label="Original requirements" value={frozen && row ? displayedIntent?.requirements || '' : c.requirements} readOnly={c.busy || frozen}
            onChange={(event) => c.setRequirements(event.target.value)} rows={5} style={{ width: '100%', boxSizing: 'border-box' }} />
        </label>
        {displayedIntent?.requirements && <p>Original request: {displayedIntent.requirements}</p>}
        {row && !displayedIntent?.requirements && <p>The saved source record does not include its original requirements or reference names.</p>}
        {c.unsupportedInputs.length > 0 && <p>Attached images remain in the composer and are unavailable for this source adapter: {c.unsupportedInputs.join(', ')}.</p>}
        <fieldset disabled={c.busy || c.materialsLoading || frozen} style={{ minWidth: 0 }}><legend>Project originals for the next source request</legend>
          {!c.materialsComplete && <p role="status">Reference list incomplete or unavailable. Staging is unavailable until the list is refreshed.</p>}
          {c.materials.map((material, index) => {
            const problem = sourceMaterialProblem(material, c.projectId, c.orgId, c.sourceContext)
            return <div key={index}><label>
              <input type="checkbox" checked={c.selected.includes(material.material_id)} disabled={!!problem}
                onChange={() => c.toggleMaterial(material.material_id)} /> {typeof material.name === 'string' && material.name ? material.name : 'Unavailable reference'}
            </label>{problem && <p>{problem}</p>}</div>
          })}
          {c.selectionProblem && <p role="alert">{c.selectionProblem}</p>}
          {!c.selectionReviewed && c.materialsComplete && <><p>{native ? 'Review the selection and choose supported TXT or JSON originals within the limits.' : 'The originals need a reviewed selection. Choose supported originals within the limits or explicitly continue with the brief only.'}</p>
            <button type="button" onClick={c.reviewSelection}>Use this reference selection</button></>}
        </fieldset>
        {displayedIntent?.referenceNames && <p>Selected originals: {displayedIntent.referenceNames.join(', ') || 'None (plain brief)'}</p>}
        <button type="button" className="chip-act" disabled={disabled || frozen} onClick={c.refreshMaterials}>Refresh references</button>
        {c.candidates.length > 1 && <div aria-label="Saved source requests">{c.candidates.map((item, index) =>
          <button key={item.source_request_id} type="button" disabled={disabled || c.unknown} onClick={() => c.selectCandidate(item.source_request_id)}>
            Source request {index + 1}: {item.state}
          </button>)}</div>}
        {c.error && <p role="alert">{c.error}</p>}
        {c.unknown && <p role="status">Publication outcome unknown. Refresh the original request.</p>}
        {c.busy && <p role="status">Preparing source action…</p>}
        {row && <div><p role="status">{row.state === 'published' ? 'Private source saved' : reviewable ? 'Source ready for review' : `Source state: ${row.state}`}</p>
          {row.state === 'published' && !native && <p>App build and private preview are still pending.</p>}
          <p>Candidate version {row.version}</p>
          <p>Candidate digest: {row.receipt_digest}</p>
          <p>Changed paths</p><ul>{row.changed_paths.map((path) => <li key={path}>{path}</li>)}</ul>
        </div>}
        {!native && c.exportBusy && <p role="status">Verifying source download…</p>}
        {downloadReady && <div><p role="status">Source download ready</p>
          <p>Git source bundle for this saved version. Private source only; app build and private preview are still pending.</p>
          <a href={c.download.url} download={c.download.filename}>Download Git source bundle</a>
          <p>SHA256: {c.download.sha256}</p>
        </div>}
        <div className="clone-dialog-actions" style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {!native && <button type="button" className="chip-act" disabled={disabled || c.unknown || !c.sourceLoaded || !exportable}
            onClick={() => c.prepareDownload(witness)}>Prepare source download</button>}
          <button type="button" className="chip-act" onClick={() => c.stage()} disabled={disabled || c.createOnlyBlocked || c.unknown || !c.sourceLoaded || !c.materialsComplete || !c.selectionReviewed || !!c.selectionProblem || !c.requirements.trim() || frozen}>Stage source</button>
          {c.secretRefusal?.overridable && <button type="button" disabled={disabled || c.createOnlyBlocked} onClick={() => c.stage({ allowSecretOnce: true })}>Send anyway</button>}
          <button type="button" className="chip-act" disabled={disabled || !reviewable} onClick={() => c.confirm(witness)}>Confirm changes</button>
          <button type="button" className="chip-act" disabled={disabled || c.unknown || row?.state !== 'publishing'} onClick={() => c.recover(witness)}>Recover</button>
          <button type="button" className="chip-act" disabled={disabled || !reviewable} onClick={() => c.reject(witness)}>Reject</button>
          <button type="button" className="chip-act" disabled={disabled} onClick={c.refresh}>Refresh source</button>
        </div>
      </div>
    </section>
  </div>
}
