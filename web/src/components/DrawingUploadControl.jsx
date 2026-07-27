import { useRef } from 'react'

export default function DrawingUploadControl({ policy, policyLoading, busy, phase, error, onUpload, onCancel }) {
  const inputRef = useRef(null)
  const accepted = (policy?.accepted || ['.dwg', '.dxf']).join(',')
  const maxMb = policy?.max_bytes ? Math.ceil(policy.max_bytes / 1024 / 1024) : null
  const choose = () => inputRef.current?.click()
  const selected = (event) => {
    const file = event.target.files?.[0]
    if (file) onUpload(file)
    event.target.value = ''
  }
  return (
    <div className="drawing-upload">
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
