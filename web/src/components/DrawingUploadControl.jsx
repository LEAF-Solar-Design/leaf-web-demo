import { useRef } from 'react'

export default function DrawingUploadControl({ policy, policyLoading, busy, phase, error, engine, onEngineChange, onUpload, onCancel }) {
  const inputRef = useRef(null)
  const accepted = (policy?.accepted || ['.dwg', '.dxf']).join(',')
  const maxMb = policy?.max_bytes ? Math.ceil(policy.max_bytes / 1024 / 1024) : null
  const choose = () => inputRef.current?.click()
  const selected = (event) => {
    const file = event.target.files?.[0]
    if (file) onUpload(file)
    event.target.value = ''
  }
  // The DWG extraction-engine toggle (operator mandate 2026-08-10): plainly
  // visible AT the point of upload, never buried in settings. Rendered from
  // the policy payload so the choices shown always match what the server
  // enforces; hidden only when the server predates the engine field.
  const engines = policy?.dwg_engines
  const localOk = policy?.dwg_local_ok !== false
  return (
    <div className="drawing-upload">
      {Array.isArray(engines) && engines.length > 0 && (
        <div className="drawing-upload-engine" role="radiogroup" aria-label="DWG extraction engine">
          <span className="drawing-upload-engine-label">DWG engine</span>
          <button
            type="button"
            role="radio"
            aria-checked={engine === 'local'}
            className={`chip-act drawing-upload-engine-opt${engine === 'local' ? ' selected' : ''}`}
            disabled={busy || policyLoading || !localOk}
            title={localOk ? 'Convert and read DWG locally with dwg2dxf — no APS cloud call' : 'Local conversion is not available on this deployment'}
            onClick={() => onEngineChange?.('local')}
          >
            Local (APS-free)
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={engine === 'aps'}
            className={`chip-act drawing-upload-engine-opt${engine === 'aps' ? ' selected' : ''}`}
            disabled={busy || policyLoading}
            title="Extract through the APS cloud service (full fidelity, paid run)"
            onClick={() => onEngineChange?.('aps')}
          >
            APS cloud
          </button>
        </div>
      )}
      <input ref={inputRef} type="file" accept={accepted} onChange={selected} aria-label="Drawing file" />
      <button type="button" className="chip-act" onClick={choose} disabled={policyLoading || busy || policy?.enabled === false}>
        {busy ? (phase === 'uploading' ? 'Uploading drawing' : 'Extracting drawing') : 'Upload DWG or DXF'}
      </button>
      {busy && <button type="button" className="tc-bar-chip" onClick={onCancel}>Cancel</button>}
      <span className="drawing-upload-note">
        {policyLoading ? 'Checking upload policy' : policy?.enabled === false ? 'Uploads unavailable' : maxMb ? `${maxMb} MB max` : ''}
      </span>
      {error && <span className="drawing-upload-error" role="alert">{error}</span>}
      {phase === 'ready' && <span className="drawing-upload-ready" role="status">Drawing ready</span>}
    </div>
  )
}
