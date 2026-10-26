import DrawingUploadControl from '../components/DrawingUploadControl.jsx'
import { humanizeError } from '../errorHumanize.js'

export function materialError(error) {
  const raw = String(error?.message || error || '')
  return humanizeError(/\bHTTP\s+\d{3}\b|\bat\s+\S+\s*\(/i.test(raw) ? null : raw)
}

export default function ProjectMaterialIntake({ project, upload = {}, intake = {}, artifacts, onStartUpload, onRetry, mock }) {
  if (mock) return <section className="ground-pane" data-pane="material-intake">
    <DrawingUploadControl disabled={!project || mock === true} />
    <p>Uploads are unavailable in this demo.</p>
    <p>This demo uses sample drawing material. Sign in to add drawings to your own project.</p>
    <a href="/app">Open workspace</a>
  </section>
  const target = intake.target?.projectName || project?.name
  const fileName = intake.target?.fileName || 'drawing'
  const failed = intake.phase === 'attach-failed' || upload.phase === 'failed'
  const error = intake.phase === 'attach-failed' ? intake.error : upload.error
  let sentence = project ? `Material attaches to ${target}` : 'Open a project first.'
  if (failed) sentence = materialError(error)
  else if (intake.phase === 'attached') sentence = `Attached ${intake.drawing?.name || fileName} as version ${intake.drawing?.version} to ${target}`
  else if (intake.phase === 'attaching') sentence = `Attaching to ${target}`
  else if (upload.phase === 'uploading') sentence = `Uploading ${fileName}`
  else if (upload.phase === 'extracting' || upload.phase === 'loading') sentence = `Extracting ${fileName}`
  return (
    <section className="ground-pane" data-pane="material-intake">
      <DrawingUploadControl {...upload} error={null} phase={upload.phase === 'ready' ? 'idle' : upload.phase}
        disabled={!project} onUpload={onStartUpload} onCancel={upload.actions?.cancel} onEngineChange={upload.actions?.setEngine} />
      <p role="status">{sentence}</p>
      {failed && <>
        <p>{intake.phase === 'attach-failed' ? 'Retry to attach this drawing to the project.' : 'Choose the drawing again to retry the upload.'}</p>
        <details><summary>Diagnostic details</summary><pre>{String(error?.message || error || '')}</pre></details>
      </>}
      {intake.beginRefused && !failed && <p role="status">{materialError(intake.beginRefused)}</p>}
      {!project && sentence !== 'Open a project first.' && <p>Open a project first.</p>}
      {intake.phase === 'attach-failed' && <button type="button" onClick={onRetry}>Retry</button>}
      {artifacts !== undefined && (artifacts.length === 0
        ? <p>No material yet. Upload a DWG or DXF.</p>
        : <ul aria-label="Project material">{artifacts.map((artifact) => (
          <li key={artifact.drawing_id}>
            <span>{artifact.name}</span>{' '}<span>{artifact.status}</span>{' '}
            <time dateTime={artifact.created_at}>{new Date(artifact.created_at).toLocaleDateString()}</time>
          </li>
        ))}</ul>)}
    </section>
  )
}
