import { useEffect, useId, useRef, useState } from 'react'
import { SOLAREDGE_PDF_MAX_BYTES, SOLAREDGE_SELECTION_ORDERS, solarEdgeReason } from './solarImportClient.js'
import { acceptParams, parseTolerance, reportSummary } from './solarEdgeImportModel.js'

// The workspace container will mount this panel; it only hands tracking params to its caller.
export default function SolarEdgeImportPanel({
  drawingId, projectId = null, graphRev, client, onReportReady, onAccept, disabled = false,
}) {
  const [file, setFile] = useState(null)
  const [source, setSource] = useState(null)
  const [report, setReport] = useState(null)
  const [tolerance, setTolerance] = useState('')
  const [order, setOrder] = useState('unknown')
  const [pending, setPending] = useState(null)
  const [outcome, setOutcome] = useState(null)
  const active = useRef(null)
  const token = useRef(0)
  const currentDrawing = useRef(drawingId)
  currentDrawing.current = drawingId
  const uploadButton = useRef(null)
  const reportButton = useRef(null)
  const acceptButton = useRef(null)
  const toleranceHint = useId()
  const acceptHint = useId()

  useEffect(() => {
    setFile(null)
    setSource(null)
    setReport(null)
    setTolerance('')
    setOrder('unknown')
    setPending(null)
    setOutcome(null)
    return () => {
      token.current += 1
      active.current?.controller.abort()
      active.current = null
    }
  }, [drawingId])

  useEffect(() => {
    if (!outcome) return
    const button = outcome.step === 'upload' ? uploadButton
      : outcome.step === 'report' ? reportButton : acceptButton
    button.current?.focus()
  }, [outcome])

  const sourceValue = source?.drawingId === drawingId ? source.value : null
  const reportValue = report?.drawingId === drawingId ? report.value : null
  const parsed = parseTolerance(tolerance)
  const params = acceptParams(graphRev, reportValue, drawingId)
  const busy = pending !== null
  const acceptReason = reportValue?.drawing_id !== drawingId
    ? 'This report belongs to another drawing. Build a report for this drawing.'
    : !Number.isInteger(graphRev) || graphRev < 0 || graphRev > 2147483647
      ? 'The current design revision is unavailable. Reopen the drawing before accepting.'
      : 'The report reference is not valid. Build the report again.'

  async function run(step) {
    if (disabled || active.current || (step === 'upload' ? !file : !sourceValue || !parsed.ok)) return
    if (step === 'upload' && file.size > SOLAREDGE_PDF_MAX_BYTES) {
      setOutcome({ step, drawingId, message: `Upload failed. ${solarEdgeReason('IMPORT_PDF_TOO_LARGE')}` })
      return
    }
    const controller = new AbortController()
    const request = { controller, token: ++token.current, drawingId }
    active.current = request
    setPending(step)
    setOutcome(null)
    // A new request makes the previous review unavailable while it is built.
    setReport(null)
    const result = step === 'upload'
      ? await client.uploadPdf({ drawingId, projectId, file, signal: controller.signal })
      : await client.requestReport({ drawingId, projectId, sourceArtifactId: sourceValue.source.artifact_id,
        alignmentTolerance: parsed.value, selectionOrder: order, signal: controller.signal })
    // Discard every result outside this call's drawing and lifetime, even if a fake ignores abort.
    if (currentDrawing.current !== request.drawingId || token.current !== request.token
      || controller.signal.aborted) return
    active.current = null
    setPending(null)
    if (!result.ok) {
      setOutcome({ step, drawingId, message: `${step === 'upload' ? 'Upload' : 'Report'} failed. ${solarEdgeReason(result.code)}${result.retryable ? '. This failure is retryable. Try again.' : ''}` })
      return
    }
    if (step === 'upload') {
      setSource({ drawingId, value: result.value })
      setOutcome({ step, drawingId, message: 'Upload succeeded. The PDF is ready for a report.' })
    } else {
      setReport({ drawingId, value: result.value })
      setOutcome({ step, drawingId, message: 'Report succeeded. Review the tracking labels before accepting.' })
      onReportReady?.(result.value)
    }
  }

  return (
    <section aria-label="SolarEdge PDF import">
      <h2>SolarEdge PDF import</h2>
      <h3>1. Upload the PDF</h3>
      <label>SolarEdge layout PDF<input key={drawingId} type="file" accept=".pdf,application/pdf"
        disabled={disabled || busy} onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
      <button ref={uploadButton} type="button" disabled={disabled || busy || !file}
        onClick={() => run('upload')}>Upload</button>
      {sourceValue && <>
        <h3>2. Build the report</h3>
        <p>Uploaded PDF: {sourceValue.page_count} pages. Source version: {sourceValue.source_version}.</p>
        <label>Alignment tolerance<input type="text" required value={tolerance}
          aria-describedby={toleranceHint} aria-invalid={!parsed.ok} disabled={disabled || busy}
          onChange={(event) => setTolerance(event.target.value)} /></label>
        <p id={toleranceHint}>{parsed.ok ? 'The alignment tolerance is within the allowed range.' : parsed.reason}</p>
        <label>Selection order<select value={order} disabled={disabled || busy}
          onChange={(event) => setOrder(event.target.value)}>
          {SOLAREDGE_SELECTION_ORDERS.map((value) => <option key={value} value={value}>{value}</option>)}
        </select></label>
        <button ref={reportButton} type="button" disabled={disabled || busy || !parsed.ok}
          onClick={() => run('report')}>Build report</button>
      </>}
      {reportValue && <section aria-label="Report review">
        <h3>3. Review the tracking labels</h3>
        <p>The report labels which panels belong to which SolarEdge string. Accepting records those labels on the design.</p>
        <p>Report source version: {reportValue.source_version}.</p>
        <table aria-label="SolarEdge report counts"><thead><tr><th scope="col">Count</th><th scope="col">Total</th></tr></thead>
          <tbody>{reportSummary(reportValue).map((row) => <tr key={row.key}><th scope="row">{row.label}</th><td>{row.value}</td></tr>)}</tbody>
        </table>
        {reportValue.counts.unassigned_panels > 0 && <p>Review needed: {reportValue.counts.unassigned_panels} panels have no tracking label.</p>}
        {reportValue.counts.partial_strings > 0 && <p>Review needed: {reportValue.counts.partial_strings} {reportValue.counts.partial_strings === 1 ? 'string is' : 'strings are'} partial.</p>}
        {!params && <p id={acceptHint}>{acceptReason}</p>}
        <button ref={acceptButton} type="button" disabled={disabled || busy || !params}
          aria-describedby={!params ? acceptHint : undefined} onClick={() => {
            if (disabled || active.current || !params) return
            onAccept?.(params)
            setOutcome({ step: 'accept', drawingId, message: 'Tracking labels sent for acceptance.' })
          }}>Accept tracking labels</button>
      </section>}
      <p role="status" aria-live="polite" aria-atomic="true">{outcome?.drawingId === drawingId ? outcome.message : ''}</p>
    </section>
  )
}
