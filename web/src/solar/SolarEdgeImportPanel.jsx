import { useEffect, useLayoutEffect, useId, useRef, useState } from 'react'
import { SOLAREDGE_PDF_MAX_BYTES, SOLAREDGE_SELECTION_ORDERS, solarEdgeReason } from './solarImportClient.js'
import { acceptParams, parseTolerance, reportSummary } from './solarEdgeImportModel.js'

export const SOLAREDGE_PANEL_REASONS = Object.freeze({
  prerequisite: 'Create panel groups before matching the PDF',
  file_required: 'Choose a SolarEdge layout PDF before uploading.',
  source_required: 'Upload the SolarEdge PDF before building its report.',
  request_pending: 'A SolarEdge request is in progress, so wait for it to finish.',
  staging_pending: 'Tracking acceptance is being staged, so wait for it to finish.',
  not_staged: 'Tracking labels were not staged for acceptance. Try again.',
})

// The workspace container will mount this panel; it only hands tracking params to its caller.
export default function SolarEdgeImportPanel({
  drawingId, projectId = null, graphRev, client, onReportReady, onAccept, disabled = false,
  drawingVersion = undefined, acceptDisabledReason = null,
}) {
  const [file, setFile] = useState(null)
  const [source, setSource] = useState(null)
  const [report, setReport] = useState(null)
  const [tolerance, setTolerance] = useState('')
  const [order, setOrder] = useState('unknown')
  const [pending, setPending] = useState(null)
  const [outcome, setOutcome] = useState(null)
  const active = useRef(null)
  const mounted = useRef(false)
  const acceptance = useRef(null)
  const acceptedReport = useRef(null)
  const [acceptPending, setAcceptPending] = useState(false)
  const token = useRef(0)
  const currentDrawing = useRef(drawingId)
  currentDrawing.current = drawingId
  const current = useRef(null)
  current.current = { drawingId, projectId, drawingVersion, disabled, acceptDisabledReason, graphRev, onAccept }
  const lifetime = useRef(null)
  if (!lifetime.current || lifetime.current.drawingId !== drawingId || lifetime.current.projectId !== projectId
    || lifetime.current.drawingVersion !== drawingVersion) lifetime.current = { drawingId, projectId, drawingVersion }
  const renderedLifetime = lifetime.current
  const renderedToken = token.current
  const uploadButton = useRef(null)
  const reportButton = useRef(null)
  const acceptButton = useRef(null)
  const toleranceHint = useId()
  const acceptHint = useId()

  useLayoutEffect(() => {
    mounted.current = true
    setFile(null)
    setSource(null)
    setReport(null)
    setTolerance('')
    setOrder('unknown')
    setPending(null)
    setAcceptPending(false)
    setOutcome(null)
    return () => {
      mounted.current = false
      token.current += 1
      active.current?.controller.abort()
      active.current = null
      acceptance.current = null
    }
  }, [drawingId, projectId])

  useLayoutEffect(() => {
    token.current += 1
    active.current?.controller.abort()
    active.current = null
    acceptance.current = null
    setPending(null)
    setAcceptPending(false)
    setOutcome(null)
  }, [drawingVersion])

  useEffect(() => {
    if (!outcome) return
    const button = outcome.step === 'upload' ? uploadButton
      : outcome.step === 'report' ? reportButton : acceptButton
    button.current?.focus()
  }, [outcome])

  const sourceValue = source?.drawingId === drawingId ? source.value : null
  const reportValue = pending === null && report?.drawingId === drawingId ? report.value : null
  const reportRef = useRef(report)
  reportRef.current = report
  const parsed = parseTolerance(tolerance)
  const params = acceptParams(graphRev, reportValue, drawingId)
  const busy = pending !== null || acceptPending
  const acceptReason = reportValue?.drawing_id !== drawingId
    ? 'This report belongs to another drawing. Build a report for this drawing.'
    : !Number.isInteger(graphRev) || graphRev < 0 || graphRev > 2147483647
      ? 'The current design revision is unavailable. Reopen the drawing before accepting.'
      : 'The report reference is not valid. Build the report again.'

  async function run(step) {
    const captured = current.current
    if (!mounted.current || lifetime.current !== renderedLifetime
      || captured.drawingId !== drawingId || captured.projectId !== projectId
      || captured.drawingVersion !== drawingVersion) return
    if (captured.disabled) return
    if (acceptance.current) { setOutcome({ step, drawingId, message: SOLAREDGE_PANEL_REASONS.staging_pending }); return }
    if (active.current) { setOutcome({ step, drawingId, message: SOLAREDGE_PANEL_REASONS.request_pending }); return }
    if (step === 'upload' && !file) { setOutcome({ step, drawingId, message: SOLAREDGE_PANEL_REASONS.file_required }); return }
    if (step === 'report' && !sourceValue) { setOutcome({ step, drawingId, message: SOLAREDGE_PANEL_REASONS.source_required }); return }
    if (step === 'report' && !parsed.ok) { setOutcome({ step, drawingId, message: parsed.reason }); return }
    if (step === 'upload' && file.size > SOLAREDGE_PDF_MAX_BYTES) {
      setOutcome({ step, drawingId, message: `Upload failed. ${solarEdgeReason('IMPORT_PDF_TOO_LARGE')}` })
      return
    }
    const controller = new AbortController()
    const request = { controller, token: ++token.current, drawingId, projectId, drawingVersion }
    active.current = request
    setPending(step)
    setOutcome(null)
    // A new request makes the previous review unavailable while it is built.
    setReport((previous) => previous ? { ...previous, eligible: false } : null)
    let result
    try {
      result = step === 'upload'
        ? await client.uploadPdf({ drawingId, projectId, file, signal: controller.signal })
        : await client.requestReport({ drawingId, projectId, sourceArtifactId: sourceValue.source.artifact_id,
          alignmentTolerance: parsed.value, selectionOrder: order, signal: controller.signal })
    } catch {
      result = { ok: false, code: 'SOLAREDGE_CLIENT_REQUEST_INVALID', retryable: false }
    }
    // Discard every result outside this call's drawing and lifetime, even if a fake ignores abort.
    if (currentDrawing.current !== request.drawingId || token.current !== request.token
      || current.current.projectId !== request.projectId || current.current.drawingVersion !== request.drawingVersion
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
      setReport({ drawingId, projectId, version: drawingVersion, eligible: true, value: result.value })
      setOutcome({ step, drawingId, message: 'Report succeeded. Review the tracking labels before accepting.' })
      onReportReady?.(result.value)
    }
  }

  function accept() {
    const captured = current.current
    if (!mounted.current || lifetime.current !== renderedLifetime || token.current !== renderedToken
      || captured.drawingId !== drawingId || captured.projectId !== projectId
      || captured.drawingVersion !== drawingVersion) return
    if (captured.disabled || captured.acceptDisabledReason || active.current) return
    if (acceptance.current) {
      setOutcome({ step: 'accept', drawingId, message: SOLAREDGE_PANEL_REASONS.staging_pending })
      return
    }
    const nextParams = acceptParams(captured.graphRev, reportValue, captured.drawingId)
    if (!nextParams || reportRef.current !== report || report?.eligible !== true
      || report?.version !== captured.drawingVersion || report?.projectId !== captured.projectId) return
    if (acceptedReport.current === reportValue) {
      setOutcome({ step: 'accept', drawingId, message: 'Tracking labels sent for acceptance.' })
      return
    }
    const lock = { generation: token.current }
    acceptance.current = lock
    const isCurrent = () => acceptance.current === lock && token.current === lock.generation
      && current.current.drawingId === captured.drawingId && current.current.projectId === captured.projectId
      && current.current.drawingVersion === captured.drawingVersion
    const finish = (accepted) => {
      if (!isCurrent()) return
      if (accepted !== false) acceptedReport.current = reportValue
      acceptance.current = null
      setAcceptPending(false)
      setOutcome({ step: 'accept', drawingId, message: accepted === false
        ? SOLAREDGE_PANEL_REASONS.not_staged : 'Tracking labels sent for acceptance.' })
    }
    let result
    try { result = captured.onAccept?.(nextParams) } catch { finish(false); return }
    if (result && typeof result.then === 'function') {
      setAcceptPending(true)
      setOutcome({ step: 'accept', drawingId, message: SOLAREDGE_PANEL_REASONS.staging_pending })
      Promise.resolve(result).then(finish, () => finish(false))
    } else finish(result)
  }

  return (
    <section aria-label="SolarEdge PDF import">
      <h2>SolarEdge PDF import</h2>
      <h3>1. Upload the PDF</h3>
      <p>{SOLAREDGE_PANEL_REASONS.prerequisite}</p>
      <label>SolarEdge layout PDF<input key={drawingId} type="file" accept=".pdf,application/pdf"
        disabled={disabled || busy} onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
      <button ref={uploadButton} type="button" disabled={disabled || busy || !file}
        onClick={() => run('upload')}>Upload</button>
      {!file && <p>{SOLAREDGE_PANEL_REASONS.file_required}</p>}
      {!sourceValue && <p>{SOLAREDGE_PANEL_REASONS.source_required}</p>}
      {pending && <p>{SOLAREDGE_PANEL_REASONS.request_pending}</p>}
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
        {!params && !acceptDisabledReason && <p id={acceptHint}>{acceptReason}</p>}
        {!acceptDisabledReason && report?.version !== drawingVersion && <p>{solarEdgeReason('SOLAREDGE_REPORT_STALE')}</p>}
        {!acceptDisabledReason && report?.version === drawingVersion && report?.eligible !== true
          && <p>{solarEdgeReason('SOLAREDGE_REPORT_REQUIRED')}</p>}
        <button ref={acceptButton} type="button" disabled={disabled || busy || !params || !!acceptDisabledReason
          || report?.version !== drawingVersion || report?.eligible !== true}
          aria-describedby={!params && !acceptDisabledReason ? acceptHint : undefined} onClick={accept}>Accept tracking labels</button>
      </section>}
      <p role="status" aria-live="polite" aria-atomic="true">{outcome?.drawingId === drawingId ? outcome.message : ''}</p>
    </section>
  )
}
