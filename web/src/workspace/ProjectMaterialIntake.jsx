import DrawingUploadControl from '../components/DrawingUploadControl.jsx'

export default function ProjectMaterialIntake({ project, upload = {}, intake = {}, artifacts, onStartUpload, onRetry, mock }) {
  const target = intake.target?.projectName || project?.name
  const fileName = intake.target?.fileName || 'drawing'
  let sentence = project ? `Material attaches to ${target}` : 'Open a project first.'
  if (intake.phase === 'attached') sentence = `Attached ${intake.drawing?.name || fileName} as version ${intake.drawing?.version} to ${target}`
  else if (intake.phase === 'attach-failed') sentence = intake.error
  else if (intake.phase === 'attaching') sentence = `Attaching to ${target}`
  else if (upload.phase === 'failed') sentence = upload.error
  else if (upload.phase === 'uploading') sentence = `Uploading ${fileName}`
  else if (upload.phase === 'extracting' || upload.phase === 'loading') sentence = `Extracting ${fileName}`
  return (
    <section className="ground-pane" data-pane="material-intake">
      <DrawingUploadControl {...upload} disabled={!project || mock === true} onUpload={onStartUpload} onCancel={upload.actions?.cancel} onEngineChange={upload.actions?.setEngine} />
      <p role="status">{sentence}</p>
      {!project && sentence !== 'Open a project first.' && <p>Open a project first.</p>}
      {mock === true && <p>Uploads are unavailable in this demo.</p>}
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
