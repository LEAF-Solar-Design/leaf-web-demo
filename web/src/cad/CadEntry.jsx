import { useRef } from 'react'

// Truthful create-or-upload entry (card C1-2). This offers EXACTLY what works
// today: upload a DWG/DXF (POST /api/drawings/upload is a real, executable
// route), or create from the blank template ONLY if that backing route is
// executable. Today it is not — server/da/blank_dwg.py backs a single
// broker-internal endpoint (POST /broker/blank-dwg/feasibility, gated by
// require_broker_reconcile_auth) that the browser can never reach, and the
// module itself is documented as "the dormant APS feasibility workflow". So
// `blankCreateAvailable` defaults to false: no dead button, no fake affordance.
// `flagEnabled` gates the whole entry (cad_upload); with it off nothing renders.
const FLAG_ENV_KEY = 'VITE_CAD_UPLOAD'

function readCadUploadFlag() {
  try {
    return import.meta.env?.[FLAG_ENV_KEY] === '1'
  } catch {
    return false
  }
}

export default function CadEntry({
  flagEnabled,
  uploadAvailable = true,
  blankCreateAvailable = false,
  accept = '.dwg,.dxf',
  busy = false,
  onUpload,
  onCreateBlank,
}) {
  const inputRef = useRef(null)
  const enabled = flagEnabled === undefined ? readCadUploadFlag() : flagEnabled

  if (!enabled) return null

  const choose = () => inputRef.current?.click()
  const selected = (event) => {
    const file = event.target.files?.[0]
    if (file) onUpload?.(file)
    event.target.value = ''
  }

  const hasAffordance = uploadAvailable || blankCreateAvailable

  return (
    <div className="cad-entry" data-testid="cad-entry">
      <h3 className="cad-entry-title">Start a CAD drawing</h3>
      {uploadAvailable && (
        <div className="cad-entry-option">
          <input
            ref={inputRef}
            type="file"
            accept={accept}
            onChange={selected}
            aria-label="Drawing file"
            style={{ display: 'none' }}
          />
          <button type="button" className="chip-act" onClick={choose} disabled={busy}>
            Upload DWG or DXF
          </button>
          <p className="cad-entry-hint">Bring an existing drawing in.</p>
        </div>
      )}
      {blankCreateAvailable && (
        <div className="cad-entry-option">
          <button type="button" className="chip-act" onClick={() => onCreateBlank?.()} disabled={busy}>
            Create from blank template
          </button>
          <p className="cad-entry-hint">Start a new drawing from scratch.</p>
        </div>
      )}
      {!hasAffordance && (
        <p className="cad-entry-empty" role="status">
          No CAD entry method is available right now.
        </p>
      )}
    </div>
  )
}
