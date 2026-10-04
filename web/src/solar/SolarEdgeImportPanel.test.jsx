import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarEdgeImportPanel, { SOLAREDGE_PANEL_REASONS } from './SolarEdgeImportPanel.jsx'
import { SOLAREDGE_IMPORT_REASONS, SOLAREDGE_PDF_MAX_BYTES,
  validateSolarEdgeReport, validateSolarEdgeSource } from './solarImportClient.js'

afterEach(cleanup)
const sourceId = 'a'.repeat(64)
const reportId = 'b'.repeat(64)
function artifact(id, drawingId, pdf) {
  return { schema: 'leaf.solar-artifact-ref.v1', artifact_id: id, media_type: pdf ? 'application/pdf' : 'application/json',
    filename: pdf ? 'solaredge-source.pdf' : 'solaredge-report.json', byte_length: 100,
    content_sha256: 'c'.repeat(64), source_version: 3, download: `/api/drawings/${drawingId}/artifacts/${id}` }
}
function source(drawingId = 'd1') {
  return { schema: 'leaf.solar-import-source.v1', kind: 'solaredge-pdf', drawing_id: drawingId,
    project_id: 'p1', source_version: 3, graph_sha256: 'd'.repeat(64), page_count: 2,
    source: artifact(sourceId, drawingId, true) }
}
function report(drawingId = 'd1') {
  return { schema: 'leaf.solar-solaredge-report-result.v1', kind: 'solaredge-report', drawing_id: drawingId,
    project_id: 'p1', source_version: 3, graph_sha256: 'd'.repeat(64), source_artifact_id: sourceId,
    counts: { pdf_matrices: 1, pdf_panels: 20, matchable_grids: 2, bridge_grids: 1, frames: 3,
      matched_frames: 2, group_strings: 4, bridge_strings: 1, strings: 5, assigned_panels: 18,
      unassigned_panels: 2, partial_strings: 1 }, report: artifact(reportId, drawingId, false) }
}
const success = (value) => ({ ok: true, status: 200, value })
const failure = (code, retryable = false) => ({ ok: false, status: 503, code, retryable })
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
function setup(overrides = {}) {
  const props = { drawingId: 'd1', projectId: 'p1', graphRev: 12, onReportReady: vi.fn(), onAccept: vi.fn(),
    client: { uploadPdf: vi.fn(async () => success(source())), requestReport: vi.fn(async () => success(report())) },
    ...overrides }
  return { ...render(<SolarEdgeImportPanel {...props} />), props }
}
const uploadButton = () => screen.getByRole('button', { name: 'Upload' })
const buildButton = () => screen.getByRole('button', { name: 'Build report' })
const acceptButton = () => screen.getByRole('button', { name: 'Accept tracking labels' })
function f3ClickHandler(button) {
  return button[Object.keys(button).find((key) => key.startsWith('__reactProps'))].onClick
}
function selectFile(file = new File(['%PDF-1.7'], 'layout.pdf', { type: 'application/pdf' })) {
  fireEvent.change(screen.getByLabelText('SolarEdge layout PDF'), { target: { files: [file] } })
  return file
}
function tolerance(text = '0.5') {
  fireEvent.change(screen.getByLabelText('Alignment tolerance'), { target: { value: text } })
}
async function upload() {
  selectFile()
  fireEvent.click(uploadButton())
  await screen.findByLabelText('Alignment tolerance')
}
async function build() {
  tolerance()
  fireEvent.click(buildButton())
  await screen.findByRole('region', { name: 'Report review' })
}

describe('SolarEdgeImportPanel', () => {
  it('SE16 accepts one report once', async () => {
    const { props } = setup({ onAccept: vi.fn(() => undefined) })
    await upload(); await build()
    fireEvent.click(acceptButton())
    expect(props.onAccept).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status').textContent).toBe('Tracking labels sent for acceptance.')
    fireEvent.click(acceptButton())
    expect(props.onAccept).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status').textContent).toBe('Tracking labels sent for acceptance.')
    await build()
    fireEvent.click(acceptButton())
    expect(props.onAccept).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('status').textContent).toBe('Tracking labels sent for acceptance.')
    props.onAccept.mockReturnValueOnce(false)
    await build()
    fireEvent.click(acceptButton())
    expect(props.onAccept).toHaveBeenCalledTimes(3)
    expect(screen.getByRole('status').textContent).toBe(SOLAREDGE_PANEL_REASONS.not_staged)
    fireEvent.click(acceptButton())
    expect(props.onAccept).toHaveBeenCalledTimes(4)
    expect(screen.getByRole('status').textContent).toBe('Tracking labels sent for acceptance.')
  })

  it('F3 17 retains review and inputs when staging returns false', async () => {
    const { props } = setup({ onAccept: vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(undefined) })
    await upload()
    const file = screen.getByLabelText('SolarEdge layout PDF').files[0]
    fireEvent.change(screen.getByLabelText('Selection order'), { target: { value: 'recorded' } })
    await build()
    const review = screen.getByRole('region', { name: 'Report review' })
    fireEvent.click(acceptButton())
    expect(screen.getByRole('status').textContent).toBe(SOLAREDGE_PANEL_REASONS.not_staged)
    expect(acceptButton().disabled).toBe(false)
    expect(screen.getByRole('region', { name: 'Report review' })).toBe(review)
    expect(screen.getByLabelText('SolarEdge layout PDF').files[0]).toBe(file)
    expect(screen.getByLabelText('Alignment tolerance').value).toBe('0.5')
    expect(screen.getByLabelText('Selection order').value).toBe('recorded')
    fireEvent.click(acceptButton())
    expect(props.onAccept).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('status').textContent).toBe('Tracking labels sent for acceptance.')
  })

  it('F3 18 locks acceptance while its promise is pending', async () => {
    const held = deferred()
    const { props } = setup({ onAccept: vi.fn(() => held.promise) })
    await upload(); await build()
    const accept = f3ClickHandler(acceptButton())
    const uploadAgain = f3ClickHandler(uploadButton())
    const reportAgain = f3ClickHandler(buildButton())
    act(() => { accept(); accept(); uploadAgain(); reportAgain() })
    expect(props.onAccept).toHaveBeenCalledTimes(1)
    expect(props.client.uploadPdf).toHaveBeenCalledTimes(1)
    expect(props.client.requestReport).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('status').textContent).toBe(SOLAREDGE_PANEL_REASONS.staging_pending)
    for (const control of [acceptButton(), uploadButton(), buildButton(), screen.getByLabelText('Selection order')]) {
      expect(control.disabled).toBe(true)
    }
    await act(async () => held.resolve(true))
    expect(screen.getByRole('status').textContent).toBe('Tracking labels sent for acceptance.')
  })

  it('F3 19 handles rejected staging and obsolete completion', async () => {
    const held = deferred()
    const { props, rerender } = setup({ drawingVersion: 3,
      onAccept: vi.fn().mockImplementationOnce(() => { throw new Error('staging') })
        .mockRejectedValueOnce(new Error('staging')).mockReturnValueOnce(held.promise) })
    await upload(); await build()
    fireEvent.click(acceptButton())
    expect(screen.getByRole('status').textContent).toBe(SOLAREDGE_PANEL_REASONS.not_staged)
    fireEvent.click(acceptButton())
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe(SOLAREDGE_PANEL_REASONS.not_staged))
    expect(acceptButton().disabled).toBe(false)
    fireEvent.click(acceptButton())
    rerender(<SolarEdgeImportPanel {...props} drawingVersion={4} />)
    await act(async () => held.resolve(true))
    expect(screen.getByRole('status').textContent).toBe('')
    expect(acceptButton().disabled).toBe(true)
    expect(screen.getByRole('region', { name: 'Report review' })).toBeTruthy()
    const next = deferred()
    props.onAccept.mockReturnValueOnce(next.promise)
    await build()
    fireEvent.click(acceptButton())
    rerender(<SolarEdgeImportPanel {...props} drawingId="d2" drawingVersion={4} />)
    await act(async () => next.resolve(false))
    expect(screen.getByRole('status').textContent).toBe('')
    expect(screen.queryByRole('region', { name: 'Report review' })).toBeNull()
  })

  it('F3 20 preserves synchronous undefined acceptance', async () => {
    const { props } = setup({ onAccept: vi.fn(() => undefined) })
    await upload(); await build()
    fireEvent.click(acceptButton())
    expect(screen.getByRole('status').textContent).toBe('Tracking labels sent for acceptance.')
    expect(document.activeElement).toBe(acceptButton())
    expect(props.onAccept).toHaveBeenCalledExactlyOnceWith({ expected_rev: 12, report_artifact_id: reportId })
  })

  it('F3 21 preserves report counts and matching inputs', async () => {
    const { props } = setup()
    await upload()
    fireEvent.change(screen.getByLabelText('Selection order'), { target: { value: 'recorded' } })
    await build()
    expect(props.client.requestReport).toHaveBeenCalledWith(expect.objectContaining({ alignmentTolerance: 0.5, selectionOrder: 'recorded' }))
    expect(within(screen.getByRole('table', { name: 'SolarEdge report counts' })).getAllByRole('row')).toHaveLength(13)
    expect(screen.getByText('Review needed: 2 panels have no tracking label.')).toBeTruthy()
    expect(screen.getByText('Review needed: 1 string is partial.')).toBeTruthy()
    expect(screen.getByLabelText('Alignment tolerance').value).toBe('0.5')
    expect(screen.getByLabelText('Selection order').value).toBe('recorded')
  })

  it('F3 22 preserves validation and refusal sentences', async () => {
    const { props } = setup()
    const large = new File(['%PDF'], 'large.pdf', { type: 'application/pdf' })
    Object.defineProperty(large, 'size', { value: SOLAREDGE_PDF_MAX_BYTES + 1 })
    selectFile(large); fireEvent.click(uploadButton())
    expect(screen.getByRole('status').textContent).toContain(SOLAREDGE_IMPORT_REASONS.IMPORT_PDF_TOO_LARGE)
    expect(props.client.uploadPdf).not.toHaveBeenCalled()
    await upload()
    tolerance('0')
    act(() => { f3ClickHandler(buildButton())() })
    expect(screen.getByRole('status').textContent).toBe('Enter an alignment tolerance from 0.000001 through 1000000.')
    expect(props.client.requestReport).not.toHaveBeenCalled()
    for (const [value, sentence] of [[report('d2'), 'This report belongs to another drawing. Build a report for this drawing.'],
      [{ ...report(), report: { ...report().report, artifact_id: 'B'.repeat(64) } }, 'The report reference is not valid. Build the report again.']]) {
      props.client.requestReport.mockResolvedValueOnce(success(value))
      await build()
      expect(screen.getByText(sentence)).toBeTruthy()
      act(() => { f3ClickHandler(acceptButton())() })
      expect(props.onAccept).not.toHaveBeenCalled()
    }
    props.client.requestReport.mockResolvedValueOnce(failure('FORBIDDEN'))
    tolerance('0.5'); fireEvent.click(buildButton())
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain(SOLAREDGE_IMPORT_REASONS.FORBIDDEN))
    expect(screen.getByLabelText('Alignment tolerance').value).toBe('0.5')
    expect(screen.getByLabelText('SolarEdge layout PDF').files[0].name).toBe('layout.pdf')
    expect(props.onAccept).not.toHaveBeenCalled()
  })

  it('SE1 uploads, builds and shows all counts from a client-valid report', async () => {
    const { props } = setup()
    expect(validateSolarEdgeSource(source(), 'd1')).toEqual(source())
    expect(validateSolarEdgeReport(report(), 'd1', sourceId)).toEqual(report())
    expect(screen.getByLabelText('SolarEdge layout PDF').accept).toBe('.pdf,application/pdf')
    await upload()
    expect(screen.getByText('Uploaded PDF: 2 pages. Source version: 3.')).toBeTruthy()
    expect(screen.getByLabelText('Alignment tolerance').value).toBe('')
    expect(screen.getByLabelText('Alignment tolerance').required).toBe(true)
    expect(screen.getByLabelText('Selection order').value).toBe('unknown')
    expect(buildButton().disabled).toBe(true)
    expect(props.onReportReady).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Accept tracking labels' })).toBeNull()
    await build()
    expect(props.client.uploadPdf).toHaveBeenCalledWith(expect.objectContaining({ drawingId: 'd1', projectId: 'p1' }))
    expect(props.client.requestReport).toHaveBeenCalledWith(expect.objectContaining({ drawingId: 'd1', projectId: 'p1',
      sourceArtifactId: sourceId, alignmentTolerance: 0.5, selectionOrder: 'unknown' }))
    expect(props.onReportReady).toHaveBeenCalledTimes(1)
    expect(props.onReportReady).toHaveBeenCalledWith(report())
    expect(acceptButton().disabled).toBe(false)
    const rows = within(screen.getByRole('table', { name: 'SolarEdge report counts' })).getAllByRole('row').slice(1)
    expect(rows.map((row) => row.textContent)).toEqual([
      'PDF matrices1', 'PDF panels20', 'Matchable grids2', 'Bridge grids1', 'Panel groups3',
      'Matched panel groups2', 'Group strings4', 'Bridge strings1', 'Strings5', 'Assigned panels18',
      'Unassigned panels2', 'Partial strings1',
    ])
    expect(screen.getByText('Report source version: 3.')).toBeTruthy()
    expect(screen.getByText('Review needed: 2 panels have no tracking label.')).toBeTruthy()
    expect(screen.getByText('Review needed: 1 string is partial.')).toBeTruthy()
  })

  it('SE2 keeps Accept absent before a report and while the report is pending', async () => {
    const pending = deferred()
    const { props } = setup()
    expect(screen.queryByRole('button', { name: 'Accept tracking labels' })).toBeNull()
    await upload()
    props.client.requestReport.mockReturnValue(pending.promise)
    tolerance()
    fireEvent.click(buildButton())
    expect(screen.queryByRole('button', { name: 'Accept tracking labels' })).toBeNull()
    expect(buildButton().disabled).toBe(true)
  })

  it.each(Object.entries(SOLAREDGE_IMPORT_REASONS))('SE3 upload refusal %s uses the client sentence and keeps the file', async (code, sentence) => {
    const { props } = setup()
    props.client.uploadPdf.mockResolvedValue(failure(code, true))
    const file = selectFile()
    fireEvent.click(uploadButton())
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain(sentence))
    expect(screen.getByRole('status').textContent).toContain('retryable')
    expect(screen.getByLabelText('SolarEdge layout PDF').files[0]).toBe(file)
    expect(uploadButton().disabled).toBe(false)
    fireEvent.click(uploadButton())
    await waitFor(() => expect(props.client.uploadPdf).toHaveBeenCalledTimes(2))
    expect(props.client.uploadPdf.mock.calls[1][0].file).toBe(file)
    expect(props.client.requestReport).not.toHaveBeenCalled()
  })

  it.each(Object.entries(SOLAREDGE_IMPORT_REASONS))('SE3 report refusal %s uses the client sentence and keeps inputs', async (code, sentence) => {
    const { props } = setup()
    await upload()
    const file = screen.getByLabelText('SolarEdge layout PDF').files[0]
    props.client.requestReport.mockResolvedValue(failure(code))
    tolerance('0.25')
    fireEvent.change(screen.getByLabelText('Selection order'), { target: { value: 'recorded' } })
    fireEvent.click(buildButton())
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain(sentence))
    expect(screen.getByLabelText('SolarEdge layout PDF').files[0]).toBe(file)
    expect(screen.getByLabelText('Alignment tolerance').value).toBe('0.25')
    expect(screen.getByLabelText('Selection order').value).toBe('recorded')
    expect(screen.getByRole('status').textContent).not.toContain('This failure is retryable.')
    fireEvent.click(buildButton())
    await waitFor(() => expect(props.client.requestReport).toHaveBeenCalledTimes(2))
    expect(props.client.requestReport.mock.calls[1][0]).toEqual(expect.objectContaining({ alignmentTolerance: 0.25, selectionOrder: 'recorded' }))
  })

  it('SE4 discards an upload after switching drawings and resets step one', async () => {
    const pending = deferred()
    const { props, rerender } = setup()
    props.client.uploadPdf.mockReturnValue(pending.promise)
    selectFile()
    fireEvent.click(uploadButton())
    const signal = props.client.uploadPdf.mock.calls[0][0].signal
    rerender(<SolarEdgeImportPanel {...props} drawingId="d2" />)
    expect(signal.aborted).toBe(true)
    await act(async () => pending.resolve(success(source())))
    expect(screen.queryByLabelText('Alignment tolerance')).toBeNull()
    expect(screen.queryByText(/Uploaded PDF:/)).toBeNull()
    expect(uploadButton().disabled).toBe(true)
    expect(screen.getByRole('status').textContent).toBe('')
    expect(props.onReportReady).not.toHaveBeenCalled()
  })

  it('SE5 discards a report after switching drawings without a callback or state change', async () => {
    const pending = deferred()
    const { props, rerender } = setup()
    await upload()
    props.client.requestReport.mockReturnValue(pending.promise)
    tolerance()
    fireEvent.click(buildButton())
    const signal = props.client.requestReport.mock.calls[0][0].signal
    rerender(<SolarEdgeImportPanel {...props} drawingId="d2" />)
    expect(signal.aborted).toBe(true)
    await act(async () => pending.resolve(success(report())))
    expect(props.onReportReady).not.toHaveBeenCalled()
    expect(screen.queryByRole('region', { name: 'Report review' })).toBeNull()
    expect(screen.queryByLabelText('Alignment tolerance')).toBeNull()
    expect(screen.getByRole('status').textContent).toBe('')
    expect(uploadButton().disabled).toBe(true)
  })

  it('SE6 locks upload and report synchronously against double clicks', async () => {
    const uploadPending = deferred()
    const reportPending = deferred()
    const { props } = setup()
    props.client.uploadPdf.mockReturnValue(uploadPending.promise)
    props.client.requestReport.mockReturnValue(reportPending.promise)
    selectFile()
    act(() => { uploadButton().click(); uploadButton().click() })
    expect(props.client.uploadPdf).toHaveBeenCalledTimes(1)
    expect(uploadButton().disabled).toBe(true)
    await act(async () => uploadPending.resolve(success(source())))
    tolerance()
    act(() => { buildButton().click(); buildButton().click() })
    expect(props.client.requestReport).toHaveBeenCalledTimes(1)
    expect(buildButton().disabled).toBe(true)
    expect(uploadButton().disabled).toBe(true)
    await act(async () => reportPending.resolve(success(report())))
    expect(props.onReportReady).toHaveBeenCalledTimes(1)
  })

  it('SE5 keeps an older report from replacing a newer call after returning to the same drawing', async () => {
    const oldReport = deferred()
    const newReport = deferred()
    const { props, rerender } = setup()
    await upload()
    props.client.requestReport.mockReturnValueOnce(oldReport.promise).mockReturnValueOnce(newReport.promise)
    tolerance()
    fireEvent.click(buildButton())
    const oldSignal = props.client.requestReport.mock.calls[0][0].signal
    rerender(<SolarEdgeImportPanel {...props} drawingId="d2" />)
    rerender(<SolarEdgeImportPanel {...props} />)
    await upload()
    tolerance()
    fireEvent.click(buildButton())
    const newSignal = props.client.requestReport.mock.calls[1][0].signal
    expect(newSignal).not.toBe(oldSignal)
    expect(newSignal.aborted).toBe(false)
    await act(async () => oldReport.resolve(success(report())))
    expect(props.onReportReady).not.toHaveBeenCalled()
    expect(buildButton().disabled).toBe(true)
    expect(screen.queryByRole('region', { name: 'Report review' })).toBeNull()
    await act(async () => newReport.resolve(success(report())))
    expect(props.onReportReady).toHaveBeenCalledTimes(1)
    expect(acceptButton().disabled).toBe(false)
  })

  it('SE7 refuses an oversized PDF before calling the client', () => {
    const { props } = setup()
    const file = new File(['%PDF'], 'large.pdf', { type: 'application/pdf' })
    Object.defineProperty(file, 'size', { value: SOLAREDGE_PDF_MAX_BYTES + 1 })
    selectFile(file)
    fireEvent.click(uploadButton())
    expect(props.client.uploadPdf).not.toHaveBeenCalled()
    expect(screen.getByRole('status').textContent).toContain(SOLAREDGE_IMPORT_REASONS.IMPORT_PDF_TOO_LARGE)
    expect(screen.getByLabelText('SolarEdge layout PDF').files[0]).toBe(file)
    expect(document.activeElement).toBe(uploadButton())
  })

  it.each(['', '0', '1e-7', '1000001', 'abc', '0x1', ' 1', 'NaN'])('SE8 disables Build report for tolerance %j', async (text) => {
    const { props } = setup()
    await upload()
    tolerance(text)
    expect(buildButton().disabled).toBe(true)
    fireEvent.click(buildButton())
    expect(props.client.requestReport).not.toHaveBeenCalled()
  })

  it.each([['1e-6', 0.000001], ['1000000', 1000000], ['0.5', 0.5]])('SE9 sends tolerance %s as a number', async (text, value) => {
    const { props } = setup()
    await upload()
    tolerance(text)
    expect(buildButton().disabled).toBe(false)
    fireEvent.click(buildButton())
    await screen.findByRole('region', { name: 'Report review' })
    expect(props.client.requestReport.mock.calls[0][0].alignmentTolerance).toBe(value)
  })

  it('SE10 passes exactly the accept tool parameters to its caller', async () => {
    const { props } = setup()
    await upload()
    await build()
    fireEvent.click(acceptButton())
    expect(props.onAccept).toHaveBeenCalledTimes(1)
    expect(props.onAccept).toHaveBeenCalledWith({ expected_rev: 12, report_artifact_id: reportId })
    expect(Object.keys(props.onAccept.mock.calls[0][0]).sort()).toEqual(['expected_rev', 'report_artifact_id'])
  })

  it('SE11 disables acceptance of a forged report for another drawing with a reason', async () => {
    const { props } = setup()
    props.client.requestReport.mockResolvedValue(success(report('d2')))
    await upload()
    await build()
    expect(acceptButton().disabled).toBe(true)
    expect(screen.getByText('This report belongs to another drawing. Build a report for this drawing.')).toBeTruthy()
    fireEvent.click(acceptButton())
    expect(props.onAccept).not.toHaveBeenCalled()
  })

  it.each([undefined, -1, 1.5, 2147483648])('SE12 disables acceptance for revision %j with a reason', async (graphRev) => {
    const { props } = setup({ graphRev })
    await upload()
    await build()
    expect(acceptButton().disabled).toBe(true)
    expect(screen.getByText('The current design revision is unavailable. Reopen the drawing before accepting.')).toBeTruthy()
    fireEvent.click(acceptButton())
    expect(props.onAccept).not.toHaveBeenCalled()
  })

  it('SE13 describes tracking labels without claiming to create circuits', async () => {
    setup()
    await upload()
    await build()
    const review = screen.getByRole('region', { name: 'Report review' })
    expect(review.textContent).toContain('The report labels which panels belong to which SolarEdge string. Accepting records those labels on the design.')
    expect(review.textContent).not.toMatch(/designed|sized|wired|electrical design/i)
  })

  it('SE1 omits review callouts when no panels are unassigned and no strings are partial', async () => {
    const { props } = setup()
    const value = report()
    value.counts.unassigned_panels = 0
    value.counts.partial_strings = 0
    props.client.requestReport.mockResolvedValue(success(value))
    await upload()
    await build()
    expect(screen.queryByText(/Review needed:/)).toBeNull()
    expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(13)
  })

  it('SE10 disables acceptance for an invalid artifact reference with a reason', async () => {
    const { props } = setup()
    const value = report()
    value.report.artifact_id = 'B'.repeat(64)
    props.client.requestReport.mockResolvedValue(success(value))
    await upload()
    await build()
    expect(acceptButton().disabled).toBe(true)
    expect(screen.getByText('The report reference is not valid. Build the report again.')).toBeTruthy()
    fireEvent.click(acceptButton())
    expect(props.onAccept).not.toHaveBeenCalled()
  })

  it('SE6 honors the external disabled prop for every step', async () => {
    const { props, rerender } = setup({ disabled: true })
    expect(screen.getByLabelText('SolarEdge layout PDF').disabled).toBe(true)
    selectFile()
    fireEvent.click(uploadButton())
    expect(props.client.uploadPdf).not.toHaveBeenCalled()
    rerender(<SolarEdgeImportPanel {...props} disabled={false} />)
    await upload()
    await build()
    rerender(<SolarEdgeImportPanel {...props} disabled />)
    expect(uploadButton().disabled).toBe(true)
    expect(buildButton().disabled).toBe(true)
    expect(acceptButton().disabled).toBe(true)
    fireEvent.click(acceptButton())
    expect(props.onAccept).not.toHaveBeenCalled()
  })

  it.each(['upload', 'report'])('SE14 aborts pending %s on unmount and ignores its late answer', async (step) => {
    const pending = deferred()
    const { props, unmount } = setup()
    if (step === 'report') await upload()
    const method = step === 'upload' ? props.client.uploadPdf : props.client.requestReport
    method.mockReturnValue(pending.promise)
    if (step === 'upload') { selectFile(); fireEvent.click(uploadButton()) }
    else { tolerance(); fireEvent.click(buildButton()) }
    const signal = method.mock.calls.at(-1)[0].signal
    expect(signal.aborted).toBe(false)
    unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => pending.resolve(success(step === 'upload' ? source() : report())))
    expect(props.onReportReady).not.toHaveBeenCalled()
    expect(props.onAccept).not.toHaveBeenCalled()
  })

  it('SE15 announces success and failure politely and returns focus to each step button', async () => {
    const { props } = setup()
    const status = screen.getByRole('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.getAttribute('aria-atomic')).toBe('true')
    props.client.uploadPdf.mockResolvedValueOnce(failure('IMPORT_PDF_MALFORMED'))
    selectFile()
    fireEvent.click(uploadButton())
    await waitFor(() => expect(status.textContent).toContain('Upload failed.'))
    expect(document.activeElement).toBe(uploadButton())
    fireEvent.click(uploadButton())
    await waitFor(() => expect(status.textContent).toContain('Upload succeeded.'))
    expect(document.activeElement).toBe(uploadButton())
    props.client.requestReport.mockResolvedValueOnce(failure('REPORT_BUSY', true))
    tolerance()
    fireEvent.click(buildButton())
    await waitFor(() => expect(status.textContent).toContain('Report failed.'))
    expect(status.textContent).toContain('retryable')
    expect(document.activeElement).toBe(buildButton())
    fireEvent.click(buildButton())
    await waitFor(() => expect(status.textContent).toContain('Report succeeded.'))
    expect(document.activeElement).toBe(buildButton())
    fireEvent.click(acceptButton())
    expect(status.textContent).toBe('Tracking labels sent for acceptance.')
    expect(document.activeElement).toBe(acceptButton())
  })
})
