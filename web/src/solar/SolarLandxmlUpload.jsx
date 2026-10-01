// The LandXML terrain upload control. Built and proven unmounted: a later mount passes the
// drawing, the checkout state and the upload client's uploadLandxml; this file imports no
// fetch, no api.js and no storage. One upload at a time, never a silent fallback: Import stays
// off with the reason shown until the client would send the file, and the answer is either the
// stored terrain's summary or the refusal sentence for its code.
import { useEffect, useRef, useState } from 'react'
import { LANDXML_DRAWING_UNITS } from './solarLandxmlClient.js'
import {
  LANDXML_UPLOAD_CELLS_HINT, LANDXML_UPLOAD_CRS_HINT, LANDXML_UPLOAD_DEFAULT_CELLS, LANDXML_UPLOAD_UNIT_LABELS,
  buildLandxmlUpload, landxmlUploadCancelled, landxmlUploadOutcome, landxmlUploadSentence,
} from './solarLandxmlUploadModel.js'

export default function SolarLandxmlUpload({ drawingId, projectId = null, upload, checkoutHeld, busy, onImported, onClose }) {
  return (
    <UploadForDrawing
      key={JSON.stringify([drawingId ?? null, projectId ?? null])}
      drawingId={drawingId}
      projectId={projectId}
      upload={upload}
      checkoutHeld={checkoutHeld}
      busy={busy}
      onImported={onImported}
      onClose={onClose}
    />
  )
}

function UploadForDrawing({ drawingId, projectId, upload, checkoutHeld, busy, onImported, onClose }) {
  const [file, setFile] = useState(null)
  const [units, setUnits] = useState('')
  const [crs, setCrs] = useState('')
  const [cells, setCells] = useState(LANDXML_UPLOAD_DEFAULT_CELLS)
  const [uploading, setUploading] = useState(false)
  const [outcome, setOutcome] = useState(null)
  // The controller of the upload in flight; a settle that is not for it is ignored.
  const active = useRef(null)
  const imported = useRef(onImported)
  imported.current = onImported

  useEffect(() => () => {
    const controller = active.current
    active.current = null
    controller?.abort()
  }, [])

  const built = buildLandxmlUpload({ drawingId, projectId, file, units, crs, cells })
  const reason = uploading ? 'upload_in_progress'
    : typeof upload !== 'function' ? 'LANDXML_CLIENT_REQUEST_INVALID'
      : busy ? 'run_in_progress'
        : checkoutHeld !== true ? 'checkout_required'
          : built.ok ? null : built.reason

  function edit(setter, value) {
    setter(value)
    setOutcome(null)
  }

  function submit(event) {
    event.preventDefault()
    // `uploading` is the lock: React renders a discrete event's update before the next event, so a second
    // submit already sees the upload_in_progress reason (UC3).
    if (reason !== null || !built.ok) return
    const controller = new AbortController()
    active.current = controller
    setUploading(true)
    setOutcome(null)
    Promise.resolve()
      .then(() => upload({ ...built.request, signal: controller.signal }))
      .then((result) => landxmlUploadOutcome(result), () => landxmlUploadOutcome(null))
      .then((next) => {
        if (active.current !== controller) return
        active.current = null
        setUploading(false)
        setOutcome(next)
        if (next.kind === 'imported' && typeof imported.current === 'function') {
          try {
            imported.current(next.value)
          } catch {
            // The mount's failure never hides the stored terrain's summary.
          }
        }
      })
  }

  function cancel() {
    const controller = active.current
    if (controller === null) return
    active.current = null
    controller.abort()
    setUploading(false)
    setOutcome(landxmlUploadCancelled())
  }

  return (
    <section aria-label="LandXML terrain import" data-testid="solar-landxml-upload"
      data-phase={uploading ? 'uploading' : outcome?.kind ?? 'idle'}>
      <form className="params" onSubmit={submit} noValidate>
        <label className="param">
          <span>LandXML file</span>
          <input type="file" accept=".xml,application/xml,text/xml" disabled={uploading}
            onChange={(event) => {
              const chosen = event.target.files?.[0] ?? null
              edit(setFile, chosen)
            }} />
        </label>
        <label className="param">
          <span>Drawing units</span>
          <select value={units} disabled={uploading} onChange={(event) => edit(setUnits, event.target.value)}>
            <option value="">Choose</option>
            {LANDXML_DRAWING_UNITS.map((unit) => <option key={unit} value={unit}>{LANDXML_UPLOAD_UNIT_LABELS[unit]}</option>)}
          </select>
        </label>
        <label className="param">
          <span>Coordinate system</span>
          <input type="text" value={crs} disabled={uploading}
            aria-invalid={!built.ok && built.field === 'crs' ? 'true' : undefined}
            onChange={(event) => edit(setCrs, event.target.value)} />
        </label>
        <p>{LANDXML_UPLOAD_CRS_HINT}</p>
        <label className="param">
          <span>Grid size</span>
          <input type="text" inputMode="numeric" value={cells} disabled={uploading}
            aria-invalid={!built.ok && built.field === 'cells' ? 'true' : undefined}
            onChange={(event) => edit(setCells, event.target.value)} />
        </label>
        <p>{LANDXML_UPLOAD_CELLS_HINT}</p>
        {reason !== null && <p role="status" data-testid="solar-landxml-reason">{landxmlUploadSentence(reason)}</p>}
        {outcome?.kind === 'refused' && <p role="alert" data-testid="solar-landxml-refusal">{outcome.text}</p>}
        {outcome?.kind === 'imported' && (
          <dl aria-label="Imported terrain" data-testid="solar-landxml-summary">
            {outcome.lines.map((line) => (
              <div key={line.key}>
                <dt>{line.label}</dt>
                <dd>{line.text}</dd>
              </div>
            ))}
          </dl>
        )}
        <button type="submit" disabled={reason !== null}>Import terrain</button>
        {uploading && <button type="button" onClick={cancel}>Cancel import</button>}
      </form>
      {typeof onClose === 'function' && (
        <button type="button" onClick={() => {
          cancel()
          onClose()
        }}>Close</button>
      )}
    </section>
  )
}
